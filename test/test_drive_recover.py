"""Pruebas del escáner de recuperación (Paso 1). Sin red: Drive simulado."""
import os

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

from app.drive_recover import escanear_en, parsear_carpeta_hijo, parsear_nombre_archivo

F = "application/vnd.google-apps.folder"


class FakeDrive:
    """Árbol {padre_id: [archivos]}; responde al q de files().list()."""
    def __init__(self, arbol):
        self.arbol = arbol
        self.escrituras = 0

    def files(self):
        return self

    def list(self, q, **kw):
        self._q = q
        return self

    def execute(self):
        if "name='Docukids'" in self._q:
            return {"files": [{"id": "RAIZ"}]}
        padre = self._q.split("'")[1]
        return {"files": self.arbol.get(padre, [])}


def test_parsear_carpeta_hijo():
    assert parsear_carpeta_hijo("12_Ana") == (12, "Ana")
    assert parsear_carpeta_hijo("3_Juan Pablo") == (3, "Juan Pablo")
    assert parsear_carpeta_hijo("Salud") is None
    assert parsear_carpeta_hijo("") is None


def test_parsear_nombre_archivo():
    assert parsear_nombre_archivo("12_Ana_Cartilla_57.pdf", "12_Ana") == "Cartilla"
    assert parsear_nombre_archivo("12_Ana_Libreta de notas_9.jpg", "12_Ana") == "Libreta de notas"
    assert parsear_nombre_archivo("foto_a_mano.png", "12_Ana") == "foto_a_mano"


def test_escanea_y_excluye_vinculadas():
    arbol = {
        "RAIZ": [
            {"id": "H12", "name": "12_Ana", "mimeType": F},
            {"id": "H13", "name": "13_Leo", "mimeType": F},
            {"id": "X", "name": "Salud", "mimeType": F},          # carpeta vieja v2.3: se ignora
            {"id": "S", "name": "suelto.pdf", "mimeType": "application/pdf"},
        ],
        "H12": [
            {"id": "SAL", "name": "Salud", "mimeType": F},
            {"id": "f0", "name": "12_Ana_DNI_1.pdf", "mimeType": "application/pdf", "webViewLink": "l0"},
        ],
        "SAL": [{"id": "f1", "name": "12_Ana_Cartilla_57.pdf", "mimeType": "application/pdf", "webViewLink": "l1"}],
        "H13": [],
    }
    drive = FakeDrive(arbol)
    r = escanear_en(drive, ya_vinculadas=["H13"])
    assert [c["drive_folder_id"] for c in r] == ["H12"]
    ana = r[0]
    assert ana["old_child_id"] == 12 and ana["name"] == "Ana"
    assert ana["files"][0]["doc_name"] == "DNI"
    assert ana["subfolders"][0]["name"] == "Salud"
    assert ana["subfolders"][0]["files"][0]["doc_name"] == "Cartilla"
    assert ana["subfolders"][0]["files"][0]["drive_link"] == "l1"


def test_sin_docukids_devuelve_vacio():
    class Vacio(FakeDrive):
        def execute(self):
            return {"files": []}
    assert escanear_en(Vacio({})) == []
