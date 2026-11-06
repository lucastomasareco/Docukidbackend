"""Pruebas de GET /drive/recover/preview y POST /drive/recover. Sin base ni red."""
import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

from app import main
from app.schemas import RecoverRequest


def _file(i, nombre, mime="application/pdf"):
    return {"drive_file_id": f"f{i}", "drive_name": f"x{i}", "doc_name": nombre,
            "mime_type": mime, "drive_link": f"link{i}"}


def _ana():
    return {"drive_folder_id": "H12", "old_child_id": 12, "name": "Ana",
            "files": [_file(0, "DNI")],
            "subfolders": [{"drive_folder_id": "SAL", "name": "Salud", "files": [_file(1, "Cartilla")]},
                           {"drive_folder_id": "VIA", "name": "Viajes", "files": [_file(2, "Pasaporte")]}]}


class DbFalsa:
    async def rollback(self):
        pass


class Entorno:
    def __init__(self, monkeypatch, escaneo, vinculadas=(), falla_documento=None, hijos_previos=()):
        self.escaneo = escaneo
        self.vinculadas = list(vinculadas)
        self.hijos, self.docs, self.subs, self.carpetas = {}, [], [], []
        self.renombradas, self.borrados, self.escaneos = [], [], []
        self.falla_documento = falla_documento
        self._sig = 100
        for h in hijos_previos:  # hijos que la persona ya tenía: (id, nombre, drive_folder_id)
            self.hijos[h[0]] = SimpleNamespace(id=h[0], name=h[1], birth_date=None, drive_folder_id=h[2])
        for i, n in enumerate(("Documentos", "Salud", "Escuela"), 1):
            self.carpetas.append(SimpleNamespace(id=i, name=n))

        async def get_linked_child_drive_folder_ids(db, uid):
            return self.vinculadas + [h.drive_folder_id for h in self.hijos.values() if h.drive_folder_id]

        async def get_folders_ensuring_base(db, uid):
            return self.carpetas

        async def get_folders_by_user(db, uid):
            return self.carpetas

        async def create_folder(db, uid, nombre, drive_folder_id=None):
            c = SimpleNamespace(id=50 + len(self.carpetas), name=nombre)
            self.carpetas.append(c)
            return c

        async def create_child(db, uid, nombre, nac):
            self._sig += 1
            h = SimpleNamespace(id=self._sig, name=nombre, birth_date=None, drive_folder_id=None)
            self.hijos[h.id] = h
            return h

        async def get_children_by_user(db, uid):
            return list(self.hijos.values())

        async def get_child_owned_by_user(db, cid, uid):
            return self.hijos.get(cid)

        async def delete_child(db, h):
            self.hijos.pop(h.id)
            self.docs = [d for d in self.docs if d["child_id"] != h.id]
            self.subs = [s for s in self.subs if s[0] != h.id]
            self.borrados.append(h.id)

        async def set_child_drive_folder_id(db, h, did):
            h.drive_folder_id = did

        async def set_child_folder_drive_id(db, cid, fid, did):
            self.subs.append((cid, fid, did))

        async def create_document(db, child_id, name, type_, **kw):
            if self.falla_documento == name:
                raise RuntimeError("boom")
            d = dict(child_id=child_id, name=name, **kw)
            self.docs.append(d)
            return SimpleNamespace(id=len(self.docs), doc=d)

        async def delete_document(db, documento):
            self.docs = [d for d in self.docs if d is not documento.doc]

        async def count_documents_to_migrate(db, uid):
            return len(self.docs)

        for nombre, fn in list(locals().items()):
            if nombre.startswith(("get_", "set_", "create_", "delete_", "count_")):
                monkeypatch.setattr(main.crud, nombre, fn)

        def escanear(usuario, vinculadas):
            self.escaneos.append(list(vinculadas))
            return [e for e in self.escaneo if e["drive_folder_id"] not in vinculadas]

        def renombrar(usuario, drive_id, nombre):
            self.renombradas.append((drive_id, nombre))

        monkeypatch.setattr(main, "escanear_carpetas_recuperables", escanear)
        monkeypatch.setattr(main, "renombrar_carpeta_en_drive", renombrar)

    def preview(self):
        return asyncio.run(main.recuperar_vista_previa(usuario=SimpleNamespace(id="u"), db=DbFalsa()))

    def recuperar(self, ids):
        return asyncio.run(main.recuperar_datos(RecoverRequest(folders=ids), usuario=SimpleNamespace(id="u"), db=DbFalsa()))


def test_preview_cuenta_documentos_y_lista_carpetas(monkeypatch):
    r = Entorno(monkeypatch, [_ana()]).preview()
    c = r.candidates[0]
    assert (c.drive_folder_id, c.name, c.old_child_id, c.documents) == ("H12", "Ana", 12, 3)
    assert c.folders == ["Salud", "Viajes"]


def test_preview_excluye_las_ya_vinculadas(monkeypatch):
    assert Entorno(monkeypatch, [_ana()], vinculadas=["H12"]).preview().candidates == []


def test_recuperar_vincula_la_carpeta_vieja_y_no_crea_otra(monkeypatch):
    e = Entorno(monkeypatch, [_ana()])
    r = e.recuperar(["H12"])
    hijo = e.hijos[r.recovered[0].id]
    assert hijo.drive_folder_id == "H12"            # apunta a la carpeta vieja (sin duplicar)
    assert (r.documents, r.skipped, r.remaining) == (3, 0, 3)
    assert e.renombradas == [("H12", f"{hijo.id}_Ana")]


def test_reutiliza_carpetas_de_la_app_por_nombre_y_crea_las_que_faltan(monkeypatch):
    e = Entorno(monkeypatch, [_ana()])
    e.recuperar(["H12"])
    nombres = [c.name for c in e.carpetas]
    assert nombres.count("Salud") == 1              # no se duplicó
    assert "Viajes" in nombres                      # la que no existía se creó
    assert {s[2] for s in e.subs} == {"SAL", "VIA"}  # subcarpetas viejas vinculadas
    por_nombre = {d["name"]: d for d in e.docs}
    assert por_nombre["DNI"]["folder_id"] is None
    assert por_nombre["Cartilla"]["folder_id"] == 2
    assert por_nombre["Cartilla"]["drive_file_id"] == "f1"


def test_es_idempotente_repetir_no_duplica(monkeypatch):
    e = Entorno(monkeypatch, [_ana()])
    e.recuperar(["H12"])
    r2 = e.recuperar(["H12"])
    assert r2.recovered == [] and r2.skipped == 1
    assert len(e.hijos) == 1 and len(e.docs) == 3


def test_id_inexistente_se_cuenta_como_saltado(monkeypatch):
    r = Entorno(monkeypatch, [_ana()]).recuperar(["NOPE", "H12"])
    assert r.skipped == 1 and len(r.recovered) == 1


def test_ids_repetidos_en_el_pedido_se_procesan_una_vez(monkeypatch):
    e = Entorno(monkeypatch, [_ana()])
    e.recuperar(["H12", "H12"])
    assert len(e.hijos) == 1


def test_si_falla_a_mitad_se_deshace_y_se_puede_reintentar(monkeypatch):
    e = Entorno(monkeypatch, [_ana()], falla_documento="Pasaporte")
    with pytest.raises(HTTPException) as exc:
        e.recuperar(["H12"])
    assert exc.value.status_code == 500
    assert e.hijos == {} and e.docs == []           # nada a medias
    e.falla_documento = None
    assert len(e.recuperar(["H12"]).recovered) == 1  # el reintento funciona


def test_falla_al_renombrar_no_rompe_la_recuperacion(monkeypatch):
    e = Entorno(monkeypatch, [_ana()])
    monkeypatch.setattr(main, "renombrar_carpeta_en_drive", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert len(e.recuperar(["H12"]).recovered) == 1


def test_pedido_vacio_o_enorme_se_rechaza():
    with pytest.raises(ValidationError):
        RecoverRequest(folders=[])
    with pytest.raises(ValidationError):
        RecoverRequest(folders=[str(i) for i in range(21)])


# ---------------- Paso 6: hijo que la persona ya había creado a mano ----------------

def test_preview_avisa_si_ya_existe_un_hijo_con_ese_nombre(monkeypatch):
    # 'ANA' y 'Aná' deben contar como el mismo nombre (sin mayúsculas ni tildes)
    e = Entorno(monkeypatch, [_ana()], hijos_previos=[(5, "  aná ", None)])
    m = e.preview().candidates[0].match
    assert (m.child_id, m.can_link) == (5, True)


def test_preview_sin_coincidencia_no_trae_match(monkeypatch):
    assert Entorno(monkeypatch, [_ana()], hijos_previos=[(5, "Leo", None)]).preview().candidates[0].match is None


def test_preview_marca_que_no_se_puede_vincular_si_el_hijo_ya_tiene_carpeta(monkeypatch):
    e = Entorno(monkeypatch, [_ana()], hijos_previos=[(5, "Ana", "CARPETA_PROPIA")])
    assert e.preview().candidates[0].match.can_link is False


def test_vincula_la_carpeta_vieja_al_hijo_existente_sin_crear_otro(monkeypatch):
    e = Entorno(monkeypatch, [_ana()], hijos_previos=[(5, "Ana", None)])
    r = e.recuperar(["H12"])
    assert list(e.hijos) == [5]                       # NO se creó un hijo repetido
    assert e.hijos[5].drive_folder_id == "H12"        # la carpeta vieja quedó vinculada
    assert (r.linked, len(r.recovered), r.documents) == (1, 1, 3)
    assert {d["child_id"] for d in e.docs} == {5}
    assert e.renombradas == [("H12", "5_Ana")]


def test_si_el_hijo_existente_ya_tiene_carpeta_no_se_mezcla_nada(monkeypatch):
    e = Entorno(monkeypatch, [_ana()], hijos_previos=[(5, "Ana", "CARPETA_PROPIA")])
    r = e.recuperar(["H12"])
    assert e.hijos[5].drive_folder_id == "CARPETA_PROPIA"   # la del hijo actual NO se toca
    assert r.linked == 0
    nuevo = e.hijos[r.recovered[0].id]
    assert nuevo.name == "Ana - recuperado" and nuevo.drive_folder_id == "H12"
    assert all(d["child_id"] == nuevo.id for d in e.docs)   # los documentos viejos van al perfil aparte


def test_dos_carpetas_viejas_con_el_mismo_nombre_no_chocan(monkeypatch):
    otra = dict(_ana(), drive_folder_id="H20", old_child_id=20,
                files=[], subfolders=[])
    e = Entorno(monkeypatch, [_ana(), otra], hijos_previos=[(5, "Ana", None)])
    r = e.recuperar(["H12", "H20"])
    nombres = sorted(h.name for h in e.hijos.values())
    assert nombres == ["Ana", "Ana - recuperado"]           # el primero se vinculó; el segundo, aparte
    assert r.linked == 1


def test_si_falla_al_vincular_a_un_existente_no_quedan_documentos_a_medias(monkeypatch):
    e = Entorno(monkeypatch, [_ana()], falla_documento="Pasaporte", hijos_previos=[(5, "Ana", None)])
    with pytest.raises(HTTPException):
        e.recuperar(["H12"])
    assert 5 in e.hijos and e.hijos[5].drive_folder_id is None   # el hijo existente sigue intacto
    assert e.docs == []                                          # sin documentos sueltos
    e.falla_documento = None
    assert e.recuperar(["H12"]).linked == 1                      # el reintento funciona
