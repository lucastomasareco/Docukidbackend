"""
drive_recover.py
----------------
Recuperar datos tras volver a registrarse (v2.8). PASO 1: escáner de SOLO LECTURA.

Contexto: al eliminar la cuenta con "conservar Drive", la base se borra pero en el Drive
de la persona queda    Docukids / {IDhijo}_{Nombre} / {Carpeta} / {IDhijo}_{Nombre}_{Doc}_{IDdoc}.ext
Al volver a registrarse, los hijos nuevos reciben IDs nuevos y _asegurar_carpeta_hijo
crearía una carpeta paralela. Este módulo encuentra las carpetas viejas para poder
vincularlas (Paso 2) en vez de duplicarlas.

El escaneo es de SOLO LECTURA. También mira la PAPELERA: si la persona eliminó la cuenta con
"mandar a la papelera", la carpeta 'Docukids' vieja está ahí y sigue siendo recuperable.
Solo `restaurar_carpeta_de_papelera` escribe en Drive (saca de la papelera lo que se recupera).
"""
import logging
import os
import re
from typing import Dict, Iterable, List, Optional

from googleapiclient.errors import HttpError

from .drive_upload import _MIME_CARPETA, _buscar_carpetas_raiz, _drive, _status
from .google_client import traducir_permisos_de_google
from .models import User

logger = logging.getLogger(__name__)

# '12_Ana' -> (12, 'Ana'). Es el formato de nombre_carpeta_hijo().
_RE_CARPETA_HIJO = re.compile(r"^(\d+)_(.+)$")
# Tope de seguridad para no escanear un Drive enorme sin fin.
_MAX_PAGINAS = 20


def parsear_carpeta_hijo(nombre: str) -> Optional[tuple]:
    """'12_Ana' -> (12, 'Ana'). Devuelve None si no tiene el formato de Docukids."""
    m = _RE_CARPETA_HIJO.match((nombre or "").strip())
    if not m:
        return None
    return int(m.group(1)), m.group(2).strip()


def parsear_nombre_archivo(nombre: str, prefijo_hijo: str) -> Optional[str]:
    """
    Nombre del documento a partir del archivo de Drive.
    '12_Ana_Cartilla_57.pdf' con prefijo '12_Ana' -> 'Cartilla'.
    Si el archivo no sigue el formato (lo subió la persona a mano), se devuelve el
    nombre sin extensión tal cual, para no perderlo.
    """
    base, _ext = os.path.splitext(nombre or "")
    if base.startswith(prefijo_hijo + "_"):
        base = base[len(prefijo_hijo) + 1:]
        base = re.sub(r"_\d+$", "", base)  # saca el _{IDdoc} del final
    return base.strip() or None


def _listar_hijos(drive_service, padre_id: str, en_papelera: bool = False) -> List[dict]:
    """
    Todo lo que cuelga de `padre_id`, paginado.
    - Normal: solo lo que NO está en la papelera.
    - en_papelera=True (el padre es una 'Docukids' que está en la papelera): lo que quedó
      adentro también figura como "en papelera" por herencia. Se devuelve solo eso
      (explicitlyTrashed=False); lo que la persona borró a mano antes (otro hijo, un documento)
      sí está "explícitamente" en la papelera y se deja afuera: no fue parte de la cuenta eliminada.
    """
    items: List[dict] = []
    token = None
    for _ in range(_MAX_PAGINAS):
        consulta = f"'{padre_id}' in parents" + ("" if en_papelera else " and trashed=false")
        resp = drive_service.files().list(
            q=consulta,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, webViewLink, explicitlyTrashed)",
            pageSize=1000,
            pageToken=token,
        ).execute()
        items.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    if en_papelera:
        items = [f for f in items if not f.get("explicitlyTrashed")]
    return items


def _archivo(f: dict, prefijo: str) -> dict:
    return {
        "drive_file_id": f["id"],
        "drive_name": f.get("name", ""),
        "doc_name": parsear_nombre_archivo(f.get("name", ""), prefijo) or "documento",
        "mime_type": f.get("mimeType"),
        "drive_link": f.get("webViewLink"),
    }


def escanear_en(drive_service, ya_vinculadas: Iterable[str] = ()) -> List[Dict]:
    """
    Núcleo testeable. Recorre 'Docukids' (las vivas Y las que están en la papelera) y devuelve
    una entrada por cada carpeta de hijo con formato '{ID}_{Nombre}' que NO esté en `ya_vinculadas`:
      {drive_folder_id, old_child_id, name, raiz_id, en_papelera,
       files:      [archivos sueltos en la carpeta del hijo],
       subfolders: [{drive_folder_id, name, files:[...]}]}
    Si hay varias carpetas 'Docukids' se recorren todas. Un mismo hijo repetido
    (mismo ID y nombre en dos lugares) se devuelve por separado: decide la persona.
    """
    vinculadas = set(ya_vinculadas)
    resultado: List[Dict] = []
    raices = [(r, False) for r in _buscar_carpetas_raiz(drive_service)] + [
        (r, True) for r in _buscar_carpetas_raiz(drive_service, en_papelera=True)
    ]
    for raiz_id, en_papelera in raices:
        for carpeta in _listar_hijos(drive_service, raiz_id, en_papelera):
            if carpeta.get("mimeType") != _MIME_CARPETA or carpeta["id"] in vinculadas:
                continue
            parseado = parsear_carpeta_hijo(carpeta.get("name", ""))
            if parseado is None:
                continue
            old_id, nombre = parseado
            prefijo = carpeta["name"].strip()
            entrada = {
                "drive_folder_id": carpeta["id"],
                "old_child_id": old_id,
                "name": nombre,
                "raiz_id": raiz_id,
                "en_papelera": en_papelera,
                "files": [],
                "subfolders": [],
            }
            for item in _listar_hijos(drive_service, carpeta["id"], en_papelera):
                if item.get("mimeType") == _MIME_CARPETA:
                    sub = {"drive_folder_id": item["id"], "name": item.get("name", ""), "files": []}
                    for f in _listar_hijos(drive_service, item["id"], en_papelera):
                        if f.get("mimeType") != _MIME_CARPETA:
                            sub["files"].append(_archivo(f, prefijo))
                    entrada["subfolders"].append(sub)
                else:
                    entrada["files"].append(_archivo(item, prefijo))
            resultado.append(entrada)
    return resultado


def restaurar_en(drive_service, entrada: Dict) -> int:
    """
    Saca de la papelera la 'Docukids' de la que viene `entrada`, la carpeta del hijo, sus
    subcarpetas y sus archivos (en ese orden: primero los padres). Si `entrada` no estaba en la
    papelera no hace nada. Lo que ya no existe (404) se salta. Devuelve cuántos elementos restauró.
    """
    if not entrada.get("en_papelera"):
        return 0
    ids = [entrada["raiz_id"], entrada["drive_folder_id"]]
    for sub in entrada["subfolders"]:
        ids.append(sub["drive_folder_id"])
        ids.extend(f["drive_file_id"] for f in sub["files"])
    ids.extend(f["drive_file_id"] for f in entrada["files"])
    restaurados = 0
    for archivo_id in ids:
        try:
            drive_service.files().update(
                fileId=archivo_id, body={"trashed": False}, fields="id"
            ).execute()
            restaurados += 1
        except HttpError as error:
            if _status(error) != 404:
                raise
    return restaurados


@traducir_permisos_de_google("Google Drive")
def escanear_carpetas_recuperables(user: User, ya_vinculadas: Iterable[str] = ()) -> List[Dict]:
    """Versión con credenciales reales del usuario (solo lectura)."""
    return escanear_en(_drive(user), ya_vinculadas)


@traducir_permisos_de_google("Google Drive")
def restaurar_carpeta_de_papelera(user: User, entrada: Dict) -> int:
    """Versión con credenciales reales del usuario: saca de la papelera lo que se va a recuperar."""
    return restaurar_en(_drive(user), entrada)
