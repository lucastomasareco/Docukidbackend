"""
Pruebas de POST /drive/migrate (Drive por hijo, v2.6, Guía §13, paso 6): la lógica de
los lotes. No necesitan base de datos ni red: la base (crud) y Drive están simulados.

Correr desde la carpeta del backend:   python -m pytest test/ -q
"""
import asyncio
import os
from types import SimpleNamespace

import pytest

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
           "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

from app import main
from app.drive_upload import RESULTADO_MIGRADO, RESULTADO_NO_EXISTE, RESULTADO_YA_ESTABA


def _doc(id_, child_id=12, folder_id=None, nombre="DNI"):
    return SimpleNamespace(id=id_, child_id=child_id, folder_id=folder_id, name=nombre,
                           drive_file_id=f"drv{id_}", drive_migrated=False)


def _hijo(id_=12, nombre="Ana"):
    return SimpleNamespace(id=id_, name=nombre, drive_folder_id=None)


class Entorno:
    """Reemplaza crud y las funciones de Drive que usa el endpoint; anota todo lo que pasa."""

    def __init__(self, monkeypatch, filas, pendientes_despues=0, carpetas=(),
                 resultados=None, vaciables=None):
        self.filas = list(filas)
        self.pendientes_despues = pendientes_despues
        self.carpetas = list(carpetas)
        self.resultados = resultados or {}   # id de documento -> resultado de migrar_archivo
        self.vaciables = vaciables or {}     # drive_folder_id -> True si se puede borrar
        self.lote_pedido = None
        self.drive_abierto = 0
        self.llamadas_migrar = []
        self.marcados = []
        self.ids_hijo_guardados = []
        self.ids_sub_guardados = []
        self.carpetas_viejas_olvidadas = []
        self.intentos_de_borrar = []

        async def get_documents_to_migrate(db, user_id, limite):
            self.lote_pedido = limite
            return self.filas

        async def get_folders_by_user(db, user_id):
            return self.carpetas

        async def get_child_folder_drive_id(db, child_id, folder_id):
            return f"sub-guardada-{child_id}-{folder_id}"

        async def set_child_drive_folder_id(db, hijo, drive_folder_id):
            self.ids_hijo_guardados.append((hijo.id, drive_folder_id))
            hijo.drive_folder_id = drive_folder_id

        async def set_child_folder_drive_id(db, child_id, folder_id, drive_id):
            self.ids_sub_guardados.append((child_id, folder_id, drive_id))

        async def mark_document_migrated(db, documento):
            self.marcados.append(documento.id)
            documento.drive_migrated = True

        async def count_documents_to_migrate(db, user_id):
            return self.pendientes_despues

        async def set_folder_drive_id(db, carpeta, drive_folder_id):
            self.carpetas_viejas_olvidadas.append(carpeta.id)
            carpeta.drive_folder_id = drive_folder_id

        for nombre, fn in list(locals().items()):
            if nombre.startswith(("get_", "set_", "mark_", "count_")):
                monkeypatch.setattr(main.crud, nombre, fn)

        def abrir_drive(usuario):
            self.drive_abierto += 1
            return "drive-falso"

        def migrar_archivo(drive, file_id, child_id, child_name, hijo_drive, folder_name,
                           sub_guardada, doc_name, doc_id):
            self.llamadas_migrar.append(dict(
                drive=drive, file_id=file_id, child_id=child_id, hijo_drive=hijo_drive,
                folder_name=folder_name, sub_guardada=sub_guardada, doc_name=doc_name, doc_id=doc_id))
            res = self.resultados.get(doc_id, RESULTADO_MIGRADO)
            if isinstance(res, Exception):
                raise res
            sub = f"sub-nueva-{doc_id}" if folder_name else None
            return res, f"hijo-nuevo-{child_id}", sub

        def borrar_carpeta_si_vacia(usuario, drive_folder_id):
            self.intentos_de_borrar.append(drive_folder_id)
            return self.vaciables.get(drive_folder_id, True)

        monkeypatch.setattr(main, "abrir_drive", abrir_drive)
        monkeypatch.setattr(main, "migrar_archivo", migrar_archivo)
        monkeypatch.setattr(main, "borrar_carpeta_si_vacia", borrar_carpeta_si_vacia)

    def correr(self):
        return asyncio.run(main.migrar_drive(usuario=SimpleNamespace(id="u"), db=None))


def _carpeta(id_, nombre, drive_folder_id=None):
    return SimpleNamespace(id=id_, name=nombre, drive_folder_id=drive_folder_id)


def test_pide_un_lote_de_diez():
    assert main._LOTE_MIGRACION == 10


def test_cuenta_migrados_y_saltados_y_marca_cada_documento(monkeypatch):
    hijo = _hijo()
    env = Entorno(
        monkeypatch,
        filas=[(_doc(1), hijo), (_doc(2), hijo), (_doc(3), hijo)],
        resultados={1: RESULTADO_MIGRADO, 2: RESULTADO_YA_ESTABA, 3: RESULTADO_NO_EXISTE},
    )
    r = env.correr()
    assert (r.migrated, r.skipped, r.remaining) == (2, 1, 0)
    assert env.lote_pedido == 10
    assert env.marcados == [1, 2, 3]       # también el que no existe: se da por revisado


def test_abre_drive_una_sola_vez_por_lote(monkeypatch):
    hijo = _hijo()
    env = Entorno(monkeypatch, filas=[(_doc(1), hijo), (_doc(2), hijo)])
    env.correr()
    assert env.drive_abierto == 1
    assert {c["drive"] for c in env.llamadas_migrar} == {"drive-falso"}


def test_sin_pendientes_no_abre_drive_ni_toca_nada(monkeypatch):
    env = Entorno(monkeypatch, filas=[])
    r = env.correr()
    assert (r.migrated, r.skipped, r.remaining) == (0, 0, 0)
    assert env.drive_abierto == 0
    assert env.marcados == []


def test_pasa_a_migrar_archivo_los_datos_correctos(monkeypatch):
    hijo = _hijo(12, "Ana")
    salud = _carpeta(5, "Salud")
    env = Entorno(monkeypatch, filas=[(_doc(57, 12, folder_id=5, nombre="Cartilla"), hijo)],
                  carpetas=[salud])
    env.correr()
    llamada = env.llamadas_migrar[0]
    assert llamada["file_id"] == "drv57"
    assert llamada["folder_name"] == "Salud"
    assert llamada["sub_guardada"] == "sub-guardada-12-5"
    assert llamada["doc_name"] == "Cartilla" and llamada["doc_id"] == 57


def test_documento_sin_carpeta_va_sin_subcarpeta(monkeypatch):
    hijo = _hijo()
    env = Entorno(monkeypatch, filas=[(_doc(1, folder_id=None), hijo)])
    env.correr()
    assert env.llamadas_migrar[0]["folder_name"] is None
    assert env.llamadas_migrar[0]["sub_guardada"] is None
    assert env.ids_sub_guardados == []


def test_guarda_los_ids_vigentes_de_las_carpetas(monkeypatch):
    hijo = _hijo(12, "Ana")
    env = Entorno(monkeypatch, filas=[(_doc(1, 12, folder_id=5), hijo)],
                  carpetas=[_carpeta(5, "Salud")])
    env.correr()
    assert env.ids_hijo_guardados == [(12, "hijo-nuevo-12")]
    assert env.ids_sub_guardados == [(12, 5, "sub-nueva-1")]


def test_el_segundo_documento_del_hijo_reutiliza_la_carpeta_ya_guardada(monkeypatch):
    hijo = _hijo(12, "Ana")
    env = Entorno(monkeypatch, filas=[(_doc(1), hijo), (_doc(2), hijo)])
    env.correr()
    assert env.llamadas_migrar[0]["hijo_drive"] is None
    assert env.llamadas_migrar[1]["hijo_drive"] == "hijo-nuevo-12"   # no se crea otra carpeta


def test_si_el_archivo_no_existe_no_se_guardan_ids_de_carpetas(monkeypatch):
    hijo = _hijo()
    env = Entorno(monkeypatch, filas=[(_doc(1, folder_id=5), hijo)], carpetas=[_carpeta(5, "Salud")],
                  resultados={1: RESULTADO_NO_EXISTE})
    env.correr()
    assert env.ids_hijo_guardados == [] and env.ids_sub_guardados == []
    assert env.marcados == [1]


def test_si_quedan_pendientes_no_se_limpian_las_carpetas_viejas(monkeypatch):
    hijo = _hijo()
    vieja = _carpeta(5, "Salud", drive_folder_id="vieja-salud")
    env = Entorno(monkeypatch, filas=[(_doc(1), hijo)], pendientes_despues=4, carpetas=[vieja])
    r = env.correr()
    assert r.remaining == 4
    assert env.intentos_de_borrar == []
    assert vieja.drive_folder_id == "vieja-salud"


def test_al_terminar_borra_las_carpetas_viejas_vacias_y_conserva_las_que_tienen_algo(monkeypatch):
    hijo = _hijo()
    vacia = _carpeta(5, "Salud", drive_folder_id="vieja-salud")
    con_algo = _carpeta(6, "Escuela", drive_folder_id="vieja-escuela")
    nunca = _carpeta(7, "Otra", drive_folder_id=None)
    env = Entorno(monkeypatch, filas=[(_doc(1), hijo)], pendientes_despues=0,
                  carpetas=[vacia, con_algo, nunca],
                  vaciables={"vieja-salud": True, "vieja-escuela": False})
    env.correr()
    assert env.intentos_de_borrar == ["vieja-salud", "vieja-escuela"]   # la que nunca tuvo, ni se mira
    assert vacia.drive_folder_id is None                                # borrada: se olvida
    assert con_algo.drive_folder_id == "vieja-escuela"                  # tenía algo: se conserva
    assert env.carpetas_viejas_olvidadas == [5]


def test_sin_documentos_pendientes_igual_limpia_las_carpetas_viejas(monkeypatch):
    vieja = _carpeta(5, "Salud", drive_folder_id="vieja-salud")
    env = Entorno(monkeypatch, filas=[], pendientes_despues=0, carpetas=[vieja])
    env.correr()
    assert vieja.drive_folder_id is None


def test_si_google_falla_a_mitad_lo_hecho_queda_marcado_y_lo_demas_no(monkeypatch):
    hijo = _hijo()
    env = Entorno(monkeypatch, filas=[(_doc(1), hijo), (_doc(2), hijo), (_doc(3), hijo)],
                  resultados={2: RuntimeError("Google falló")})
    with pytest.raises(RuntimeError):
        env.correr()
    assert env.marcados == [1]            # el 2 y el 3 siguen pendientes: se vuelve a llamar
    assert env.intentos_de_borrar == []   # y no se limpió nada
