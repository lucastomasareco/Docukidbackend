"""
Pruebas de Drive por hijo (v2.6, Guía §13, paso 2): armado de nombres y lógica
de carpetas. No necesitan base de datos ni red: usan un "Drive" falso en memoria.

Correr desde la carpeta del backend:   python -m pytest test/ -q
"""
import os

# google_client.py importa config.py, que exige estas variables. Valores falsos.
for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
           "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

import pytest
from googleapiclient.errors import HttpError

from app import drive_upload as du


# ---------------------------------------------------------------------------
# Armado de nombres (funciones puras)
# ---------------------------------------------------------------------------

def test_nombre_carpeta_hijo():
    assert du.nombre_carpeta_hijo(12, "Ana") == "12_Ana"


def test_dos_hijos_con_el_mismo_nombre_tienen_carpetas_distintas():
    assert du.nombre_carpeta_hijo(12, "Ana") != du.nombre_carpeta_hijo(13, "Ana")


def test_nombre_de_archivo_completo():
    assert du.armar_nombre_archivo(12, "Ana", "Cartilla", 57, "pdf") == "12_Ana_Cartilla_57.pdf"


def test_dos_hijos_iguales_con_el_mismo_documento_no_chocan():
    a = du.armar_nombre_archivo(12, "Ana", "DNI", 57, "pdf")
    b = du.armar_nombre_archivo(13, "Ana", "DNI", 58, "pdf")
    assert a != b


def test_nombre_provisional_sin_id_de_documento():
    # En la subida todavía no existe el ID del documento.
    assert du.armar_nombre_archivo(12, "Ana", "Cartilla", None, "pdf") == "12_Ana_Cartilla.pdf"


def test_sin_extension():
    assert du.armar_nombre_archivo(12, "Ana", "Cartilla", 57, None) == "12_Ana_Cartilla_57"


def test_no_repite_la_extension():
    assert du.armar_nombre_archivo(12, "Ana", "DNI.pdf", 57, "pdf") == "12_Ana_DNI_57.pdf"
    assert du.armar_nombre_archivo(12, "Ana", "Foto.JPG", 9, "jpg") == "12_Ana_Foto_9.jpg"


def test_caracteres_peligrosos_se_limpian():
    nombre = du.armar_nombre_archivo(12, "Ana/../x", "a\\b:c*d", 1, "pdf")
    for malo in ("/", "\\", ":", "*"):
        assert malo not in nombre


def test_nombre_vacio_usa_valor_por_defecto():
    assert du.armar_nombre_archivo(12, "Ana", "   ", 5, "png") == "12_Ana_documento_5.png"


def test_nunca_supera_255_caracteres_y_conserva_id_y_extension():
    nombre = du.armar_nombre_archivo(12, "A" * 500, "D" * 500, 57, "pdf")
    assert len(nombre) <= 255
    assert nombre.endswith("_57.pdf")
    assert nombre.startswith("12_")


def test_extensiones_por_tipo_real():
    assert du.EXTENSION_POR_MIME["application/pdf"] == "pdf"
    assert du.EXTENSION_POR_MIME["image/jpeg"] == "jpg"
    assert du.EXTENSION_POR_MIME["image/png"] == "png"
    assert du.EXTENSION_POR_MIME["image/webp"] == "webp"
    assert du.EXTENSION_POR_MIME["image/heic"] == "heic"


# ---------------------------------------------------------------------------
# "Drive" falso en memoria
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status):
        self.status = status
        # Las versiones nuevas de googleapiclient leen resp.reason al armar un HttpError.
        self.reason = "Error simulado"


def _error(status):
    return HttpError(_Resp(status), b"{}")


class _Ejecutable:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Archivos:
    def __init__(self, drive):
        self.drive = drive

    def list(self, q=None, spaces=None, fields=None, pageSize=None):
        def run():
            if q and " in parents" in q:  # contenido de una carpeta: '<id>' in parents and trashed=false
                padre = q.split("'")[1]
                if padre not in self.drive.items:
                    raise _error(404)
                return {"files": [{"id": i} for i, f in self.drive.items.items()
                                  if padre in f.get("parents", []) and not f.get("trashed")]}
            # Si no, se busca la raíz "Docukids".
            return {"files": [{"id": i, "name": f["name"]} for i, f in self.drive.items.items()
                              if f["name"] == "Docukids" and not f.get("trashed")]}
        return _Ejecutable(run)

    def delete(self, fileId):
        def run():
            if fileId not in self.drive.items:
                raise _error(404)
            del self.drive.items[fileId]
        return _Ejecutable(run)

    def get(self, fileId, fields=None):
        def run():
            if fileId not in self.drive.items:
                raise _error(404)
            f = self.drive.items[fileId]
            return {"id": fileId, "name": f.get("name"), "mimeType": f.get("mime"),
                    "trashed": f.get("trashed", False), "parents": f.get("parents", [])}
        return _Ejecutable(run)

    def create(self, body, media_body=None, fields=None):
        def run():
            self.drive.contador += 1
            nuevo_id = f"id{self.drive.contador}"
            self.drive.items[nuevo_id] = {"name": body["name"], "parents": body.get("parents", []),
                                          "mime": body.get("mimeType")}
            return {"id": nuevo_id, "webViewLink": f"https://drive/{nuevo_id}"}
        return _Ejecutable(run)

    def update(self, fileId, body=None, fields=None, addParents=None, removeParents=None, **kw):
        def run():
            if fileId not in self.drive.items:
                raise _error(404)
            item = self.drive.items[fileId]
            if body and "name" in body:
                item["name"] = body["name"]
            if removeParents:
                item["parents"] = [p for p in item.get("parents", []) if p not in removeParents.split(",")]
            if addParents:
                item["parents"] = item.get("parents", []) + [addParents]
            return {"id": fileId}
        return _Ejecutable(run)


class DriveFalso:
    def __init__(self):
        self.items = {}
        self.contador = 0

    def files(self):
        return _Archivos(self)


def _raiz(drive):
    return du._get_or_create_carpeta(drive)


# ---------------------------------------------------------------------------
# Carpeta del hijo
# ---------------------------------------------------------------------------

def test_crea_la_carpeta_del_hijo_dentro_de_docukids():
    drive = DriveFalso()
    hijo_id = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    carpeta = drive.items[hijo_id]
    assert carpeta["name"] == "12_Ana"
    assert carpeta["parents"] == [_raiz(drive)]


def test_reutiliza_la_carpeta_guardada_si_existe():
    drive = DriveFalso()
    primero = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    segundo = du._asegurar_carpeta_hijo(drive, 12, "Ana", primero)
    assert primero == segundo


def test_recrea_la_carpeta_si_la_borraron_o_esta_en_la_papelera():
    drive = DriveFalso()
    original = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    drive.items[original]["trashed"] = True
    nueva = du._asegurar_carpeta_hijo(drive, 12, "Ana", original)
    assert nueva != original
    del drive.items[nueva]
    otra = du._asegurar_carpeta_hijo(drive, 12, "Ana", nueva)  # id que ya no existe (404)
    assert otra != nueva


def test_dos_hijos_con_el_mismo_nombre_tienen_carpetas_distintas_en_drive():
    drive = DriveFalso()
    a = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    b = du._asegurar_carpeta_hijo(drive, 13, "Ana", None)
    assert a != b
    assert drive.items[a]["name"] != drive.items[b]["name"]


# ---------------------------------------------------------------------------
# Subcarpeta dentro del hijo
# ---------------------------------------------------------------------------

def test_subcarpeta_se_crea_dentro_de_la_carpeta_del_hijo():
    drive = DriveFalso()
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    sub = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", None)
    assert drive.items[sub]["name"] == "Salud"
    assert drive.items[sub]["parents"] == [hijo]


def test_subcarpeta_guardada_se_reutiliza():
    drive = DriveFalso()
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    sub = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", None)
    assert du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", sub) == sub


def test_subcarpeta_de_otro_hijo_nunca_se_reutiliza():
    # Aunque el id guardado exista y viva, si cuelga de OTRO hijo no se usa.
    drive = DriveFalso()
    ana = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    luis = du._asegurar_carpeta_hijo(drive, 13, "Luis", None)
    sub_ana = du._asegurar_subcarpeta_de_hijo(drive, ana, "Salud", None)
    sub_luis = du._asegurar_subcarpeta_de_hijo(drive, luis, "Salud", sub_ana)
    assert sub_luis != sub_ana
    assert drive.items[sub_luis]["parents"] == [luis]


def test_subcarpeta_borrada_se_recrea():
    drive = DriveFalso()
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    sub = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", None)
    del drive.items[sub]
    nueva = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", sub)
    assert nueva != sub
    assert drive.items[nueva]["parents"] == [hijo]


# ---------------------------------------------------------------------------
# Renombrar archivo
# ---------------------------------------------------------------------------

def test_renombrar_archivo(monkeypatch):
    drive = DriveFalso()
    archivo_id, _ = du._subir(drive, _raiz(drive), b"x", "12_Ana_Cartilla.pdf", "application/pdf")
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    assert du.renombrar_archivo_en_drive(None, archivo_id, "12_Ana_Cartilla_57.pdf") is True
    assert drive.items[archivo_id]["name"] == "12_Ana_Cartilla_57.pdf"


def test_renombrar_archivo_inexistente_devuelve_false(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    assert du.renombrar_archivo_en_drive(None, "no-existe", "x.pdf") is False


# ---------------------------------------------------------------------------
# Subida completa a la ruta nueva
# ---------------------------------------------------------------------------

def test_subida_con_carpeta_de_la_app_queda_en_hijo_y_subcarpeta(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    nombre = du.armar_nombre_archivo(12, "Ana", "Cartilla", None, "pdf")
    archivo_id, link, hijo_id, sub_id = du.upload_file_to_child_folder(
        None, b"%PDF", nombre, "application/pdf", 12, "Ana", None, "Salud", None
    )
    assert drive.items[hijo_id]["name"] == "12_Ana"
    assert drive.items[sub_id]["name"] == "Salud"
    assert drive.items[sub_id]["parents"] == [hijo_id]
    assert drive.items[archivo_id]["parents"] == [sub_id]
    assert link.startswith("https://")


def test_subida_sin_carpeta_queda_directo_en_la_carpeta_del_hijo(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    archivo_id, _, hijo_id, sub_id = du.upload_file_to_child_folder(
        None, b"x", "12_Ana_DNI.pdf", "application/pdf", 12, "Ana", None
    )
    assert sub_id is None
    assert drive.items[archivo_id]["parents"] == [hijo_id]


def test_hermanos_con_el_mismo_nombre_y_el_mismo_documento_no_se_mezclan(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    resultados = []
    for child_id in (12, 13):  # los dos se llaman "Ana" y suben un "DNI" a "Salud"
        nombre = du.armar_nombre_archivo(child_id, "Ana", "DNI", None, "pdf")
        resultados.append(du.upload_file_to_child_folder(
            None, b"x", nombre, "application/pdf", child_id, "Ana", None, "Salud", None))
    (arch_a, _, hijo_a, sub_a), (arch_b, _, hijo_b, sub_b) = resultados
    assert hijo_a != hijo_b and sub_a != sub_b
    assert drive.items[arch_a]["parents"] != drive.items[arch_b]["parents"]
    assert drive.items[arch_a]["name"] != drive.items[arch_b]["name"]


# ---------------------------------------------------------------------------
# Mover un documento (PATCH con folder_id): siempre dentro del MISMO hijo
# ---------------------------------------------------------------------------

def test_mover_a_otra_subcarpeta_del_mismo_hijo(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    archivo_id, _, hijo_id, sub_salud = du.upload_file_to_child_folder(
        None, b"x", "12_Ana_DNI.pdf", "application/pdf", 12, "Ana", None, "Salud", None)
    hijo_vigente, sub_escuela = du.mover_archivo_a_carpeta_de_hijo(
        None, archivo_id, 12, "Ana", hijo_id, "Escuela", None)
    assert hijo_vigente == hijo_id
    assert drive.items[sub_escuela]["parents"] == [hijo_id]
    assert drive.items[archivo_id]["parents"] == [sub_escuela]


def test_mover_a_sin_carpeta_va_a_la_carpeta_del_hijo_no_a_la_raiz(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    archivo_id, _, hijo_id, _ = du.upload_file_to_child_folder(
        None, b"x", "12_Ana_DNI.pdf", "application/pdf", 12, "Ana", None, "Salud", None)
    hijo_vigente, sub = du.mover_archivo_a_carpeta_de_hijo(
        None, archivo_id, 12, "Ana", hijo_id, None, None)
    assert sub is None
    assert drive.items[archivo_id]["parents"] == [hijo_id]


def test_mover_nunca_lleva_el_archivo_a_la_carpeta_de_un_hermano(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    arch_ana, _, hijo_ana, _ = du.upload_file_to_child_folder(
        None, b"x", "12_Ana_DNI.pdf", "application/pdf", 12, "Ana", None, "Salud", None)
    _, _, hijo_luis, sub_luis = du.upload_file_to_child_folder(
        None, b"x", "13_Ana_DNI.pdf", "application/pdf", 13, "Ana", None, "Salud", None)
    # Mover el archivo de Ana usando (por error) el id de la subcarpeta de Luis: no se usa.
    _, sub_nueva = du.mover_archivo_a_carpeta_de_hijo(
        None, arch_ana, 12, "Ana", hijo_ana, "Salud", sub_luis)
    assert sub_nueva != sub_luis
    assert drive.items[sub_nueva]["parents"] == [hijo_ana]
    assert drive.items[arch_ana]["parents"] == [sub_nueva]


def test_mover_archivo_que_ya_no_existe_no_falla(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    hijo_vigente, sub = du.mover_archivo_a_carpeta_de_hijo(
        None, "no-existe", 12, "Ana", None, "Salud", None)
    assert drive.items[hijo_vigente]["name"] == "12_Ana"
    assert drive.items[sub]["parents"] == [hijo_vigente]


# ---------------------------------------------------------------------------
# Renombrar y borrar hijo (paso 4)
# ---------------------------------------------------------------------------

def test_renombrar_hijo_renombra_su_carpeta_y_mantiene_el_id(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    du.renombrar_carpeta_en_drive(None, hijo, du.nombre_carpeta_hijo(12, "Ana María"))
    assert drive.items[hijo]["name"] == "12_Ana María"


def test_hijo_sin_carpeta_creada_usa_el_nombre_nuevo_al_primer_uso():
    drive = DriveFalso()
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana María", None)
    assert drive.items[hijo]["name"] == "12_Ana María"


def test_borrar_hijo_vacio_borra_su_carpeta_y_sus_subcarpetas(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    salud = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", None)
    escuela = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Escuela", None)
    assert du.borrar_carpeta_de_hijo_si_vacia(None, hijo, [salud, escuela]) is True
    assert hijo not in drive.items and salud not in drive.items and escuela not in drive.items


def test_borrar_hijo_no_toca_a_su_hermano(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    ana = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    luis = du._asegurar_carpeta_hijo(drive, 13, "Ana", None)
    sub_luis = du._asegurar_subcarpeta_de_hijo(drive, luis, "Salud", None)
    du.borrar_carpeta_de_hijo_si_vacia(None, ana, [])
    assert ana not in drive.items
    assert luis in drive.items and sub_luis in drive.items


def test_borrar_hijo_conserva_lo_que_la_persona_puso_a_mano(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    salud = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", None)
    escuela = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Escuela", None)
    manual, _ = du._subir(drive, salud, b"x", "mi_foto.jpg", "image/jpeg")  # archivo puesto a mano
    assert du.borrar_carpeta_de_hijo_si_vacia(None, hijo, [salud, escuela]) is False
    assert manual in drive.items            # el archivo sigue
    assert salud in drive.items             # su subcarpeta también
    assert hijo in drive.items              # y la carpeta del hijo
    assert escuela not in drive.items       # la subcarpeta vacía sí se limpió


def test_borrar_hijo_con_carpeta_que_ya_no_existe_no_falla(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    assert du.borrar_carpeta_de_hijo_si_vacia(None, "no-existe", ["tampoco"]) is True


def test_borrar_hijo_que_nunca_subio_nada_no_hace_nada(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    assert du.borrar_carpeta_de_hijo_si_vacia(None, None, []) is False


# ---------------------------------------------------------------------------
# Carpetas de la app (paso 5): renombrar y borrar por hijo
# ---------------------------------------------------------------------------

def _hijo_con_archivo(drive, child_id, nombre="Ana", carpeta="Salud"):
    archivo, _, hijo, sub = du.upload_file_to_child_folder(
        None, b"x", f"{child_id}_{nombre}_DNI.pdf", "application/pdf",
        child_id, nombre, None, carpeta, None)
    return archivo, hijo, sub


def test_renombrar_carpeta_renombra_la_subcarpeta_de_cada_hijo(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    _, hijo_a, sub_a = _hijo_con_archivo(drive, 12)
    _, hijo_b, sub_b = _hijo_con_archivo(drive, 13)
    for sub in (sub_a, sub_b):  # lo que hace PATCH /folders: una llamada por hijo
        du.renombrar_carpeta_en_drive(None, sub, "Médico")
    assert drive.items[sub_a]["name"] == drive.items[sub_b]["name"] == "Médico"
    assert drive.items[sub_a]["parents"] == [hijo_a]   # siguen dentro de su hijo
    assert drive.items[sub_b]["parents"] == [hijo_b]


def test_borrar_carpeta_mueve_los_archivos_a_la_carpeta_de_su_hijo(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    archivo_a, hijo_a, sub_a = _hijo_con_archivo(drive, 12)
    archivo_b, hijo_b, sub_b = _hijo_con_archivo(drive, 13)
    vigente = du.borrar_subcarpeta_de_hijo(None, 12, "Ana", hijo_a, sub_a, [archivo_a])
    assert vigente == hijo_a
    assert sub_a not in drive.items                      # subcarpeta borrada (quedó vacía)
    assert drive.items[archivo_a]["parents"] == [hijo_a]  # archivo NO borrado, en su hijo
    assert sub_b in drive.items and drive.items[archivo_b]["parents"] == [sub_b]  # hermano intacto


def test_borrar_carpeta_nunca_lleva_archivos_a_la_raiz_ni_a_un_hermano(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    archivo_a, hijo_a, sub_a = _hijo_con_archivo(drive, 12)
    _, hijo_b, _ = _hijo_con_archivo(drive, 13)
    du.borrar_subcarpeta_de_hijo(None, 12, "Ana", hijo_a, sub_a, [archivo_a])
    assert drive.items[archivo_a]["parents"] != [_raiz(drive)]
    assert drive.items[archivo_a]["parents"] != [hijo_b]


def test_borrar_carpeta_conserva_la_subcarpeta_si_tiene_algo_a_mano(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    archivo, hijo, sub = _hijo_con_archivo(drive, 12)
    manual, _ = du._subir(drive, sub, b"x", "mi_nota.txt", "text/plain")
    du.borrar_subcarpeta_de_hijo(None, 12, "Ana", hijo, sub, [archivo])
    assert manual in drive.items and sub in drive.items   # lo de la persona se conserva
    assert drive.items[archivo]["parents"] == [hijo]      # el documento salió de ahí


def test_borrar_carpeta_sin_documentos_solo_borra_la_subcarpeta(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    sub = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", None)
    assert du.borrar_subcarpeta_de_hijo(None, 12, "Ana", hijo, sub, []) == hijo
    assert sub not in drive.items and hijo in drive.items


def test_borrar_carpeta_con_archivo_que_ya_no_existe_no_falla(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    hijo = du._asegurar_carpeta_hijo(drive, 12, "Ana", None)
    sub = du._asegurar_subcarpeta_de_hijo(drive, hijo, "Salud", None)
    du.borrar_subcarpeta_de_hijo(None, 12, "Ana", hijo, sub, ["no-existe"])
    assert sub not in drive.items


def test_borrar_carpeta_vieja_solo_si_esta_vacia(monkeypatch):
    drive = DriveFalso()
    monkeypatch.setattr(du, "_drive", lambda user: drive)
    vieja = du._crear_subcarpeta(drive, "Salud")           # estructura anterior a la v2.6
    drive.contador += 0
    du._subir(drive, vieja, b"x", "algo.pdf", "application/pdf")
    assert du.borrar_carpeta_si_vacia(None, vieja) is False and vieja in drive.items
    vacia = du._crear_subcarpeta(drive, "Escuela")
    assert du.borrar_carpeta_si_vacia(None, vacia) is True and vacia not in drive.items


# ---------------------------------------------------------------------------
# Migración de documentos viejos (paso 6): migrar_archivo
# ---------------------------------------------------------------------------

def _archivo_viejo(drive, nombre, mime="application/pdf", padre=None):
    """Crea un archivo como los de antes de la v2.6: suelto en 'Docukids', sin extensión ni ID."""
    padre = padre or _raiz(drive)
    nuevo = drive.files().create(
        body={"name": nombre, "parents": [padre], "mimeType": mime}).execute()
    return nuevo["id"]


def _migrar(drive, archivo_id, hijo=(12, "Ana"), carpeta=None, doc=("DNI", 57),
           hijo_drive=None, sub_drive=None):
    return du.migrar_archivo(drive, archivo_id, hijo[0], hijo[1], hijo_drive,
                             carpeta, sub_drive, doc[0], doc[1])


def test_migra_un_archivo_suelto_a_la_carpeta_del_hijo():
    drive = DriveFalso()
    archivo = _archivo_viejo(drive, "Ana - DNI")
    resultado, hijo_id, sub_id = _migrar(drive, archivo)
    assert resultado == du.RESULTADO_MIGRADO
    assert sub_id is None
    assert drive.items[hijo_id]["name"] == "12_Ana"
    assert drive.items[archivo]["parents"] == [hijo_id]
    assert drive.items[archivo]["name"] == "12_Ana_DNI_57.pdf"


def test_migra_un_archivo_con_carpeta_a_la_subcarpeta_del_hijo():
    drive = DriveFalso()
    salud_vieja = du._crear_carpeta_en(drive, "Salud", _raiz(drive))
    archivo = _archivo_viejo(drive, "Ana - Cartilla", padre=salud_vieja)
    resultado, hijo_id, sub_id = _migrar(drive, archivo, carpeta="Salud", doc=("Cartilla", 58))
    assert resultado == du.RESULTADO_MIGRADO
    assert drive.items[sub_id]["name"] == "Salud"
    assert drive.items[sub_id]["parents"] == [hijo_id]
    assert drive.items[archivo]["parents"] == [sub_id]          # sale de la carpeta vieja
    assert drive.items[archivo]["name"] == "12_Ana_Cartilla_58.pdf"


def test_la_extension_sale_del_tipo_que_informa_drive():
    drive = DriveFalso()
    foto = _archivo_viejo(drive, "Ana - Foto", mime="image/jpeg")
    _migrar(drive, foto, doc=("Foto", 60))
    assert drive.items[foto]["name"] == "12_Ana_Foto_60.jpg"


def test_tipo_desconocido_migra_sin_extension():
    drive = DriveFalso()
    raro = _archivo_viejo(drive, "Ana - Raro", mime="application/x-raro")
    resultado, _, _ = _migrar(drive, raro, doc=("Raro", 61))
    assert resultado == du.RESULTADO_MIGRADO
    assert drive.items[raro]["name"] == "12_Ana_Raro_61"


def test_migrar_dos_veces_la_segunda_ya_estaba():
    drive = DriveFalso()
    archivo = _archivo_viejo(drive, "Ana - DNI")
    _, hijo_id, _ = _migrar(drive, archivo)
    antes = {k: dict(v) for k, v in drive.items.items()}
    resultado, hijo_2, sub_2 = _migrar(drive, archivo, hijo_drive=hijo_id)
    assert resultado == du.RESULTADO_YA_ESTABA
    assert hijo_2 == hijo_id
    assert drive.items == antes                                  # no se tocó nada


def test_migrar_reutiliza_la_carpeta_del_hijo_y_no_crea_otra():
    drive = DriveFalso()
    a = _archivo_viejo(drive, "Ana - DNI")
    b = _archivo_viejo(drive, "Ana - Partida")
    _, hijo_id, _ = _migrar(drive, a)
    _, hijo_2, _ = _migrar(drive, b, doc=("Partida", 58), hijo_drive=hijo_id)
    assert hijo_2 == hijo_id
    assert sum(1 for f in drive.items.values() if f["name"] == "12_Ana") == 1


def test_archivo_que_no_existe_se_salta_sin_crear_carpetas():
    drive = DriveFalso()
    _raiz(drive)
    antes = {k: dict(v) for k, v in drive.items.items()}
    resultado, hijo_id, sub_id = _migrar(drive, "no-existe", hijo_drive="h", sub_drive="s",
                                         carpeta="Salud")
    assert resultado == du.RESULTADO_NO_EXISTE
    assert (hijo_id, sub_id) == ("h", "s")                       # devuelve los ids tal como llegaron
    assert drive.items == antes                                  # ni carpetas nuevas ni cambios


def test_archivo_en_la_papelera_se_salta():
    drive = DriveFalso()
    archivo = _archivo_viejo(drive, "Ana - DNI")
    drive.items[archivo]["trashed"] = True
    resultado, _, _ = _migrar(drive, archivo)
    assert resultado == du.RESULTADO_NO_EXISTE
    assert drive.items[archivo]["name"] == "Ana - DNI"           # intacto


def test_dos_hijos_con_el_mismo_nombre_no_se_mezclan_al_migrar():
    drive = DriveFalso()
    dni_a = _archivo_viejo(drive, "Ana - DNI")
    dni_b = _archivo_viejo(drive, "Ana - DNI")
    _, carpeta_a, _ = _migrar(drive, dni_a, hijo=(12, "Ana"), doc=("DNI", 57))
    _, carpeta_b, _ = _migrar(drive, dni_b, hijo=(13, "Ana"), doc=("DNI", 58))
    assert carpeta_a != carpeta_b
    assert drive.items[dni_a]["parents"] == [carpeta_a]
    assert drive.items[dni_b]["parents"] == [carpeta_b]
    assert drive.items[dni_a]["name"] == "12_Ana_DNI_57.pdf"
    assert drive.items[dni_b]["name"] == "13_Ana_DNI_58.pdf"


def test_si_la_carpeta_guardada_del_hijo_ya_no_existe_se_crea_otra():
    drive = DriveFalso()
    archivo = _archivo_viejo(drive, "Ana - DNI")
    resultado, hijo_id, _ = _migrar(drive, archivo, hijo_drive="borrada")
    assert resultado == du.RESULTADO_MIGRADO
    assert hijo_id != "borrada"
    assert drive.items[archivo]["parents"] == [hijo_id]


def test_error_de_google_distinto_de_404_se_propaga(monkeypatch):
    drive = DriveFalso()
    archivo = _archivo_viejo(drive, "Ana - DNI")
    original = _Archivos.update

    def falla(self, *a, **k):
        def run():
            raise _error(500)
        return _Ejecutable(run)

    monkeypatch.setattr(_Archivos, "update", falla)
    with pytest.raises(HttpError):
        _migrar(drive, archivo)
    monkeypatch.setattr(_Archivos, "update", original)
    assert drive.items[archivo]["name"] == "Ana - DNI"           # nada quedó a medias
