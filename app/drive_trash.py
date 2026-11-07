"""
drive_trash.py
--------------
Papelera de documentos (v3.0), apoyada en la papelera de Google Drive.

Idea: al borrar un documento, su archivo NO se elimina para siempre: se manda a la papelera de
Drive (donde Google lo guarda 30 días) y el registro de la base se borra como siempre. Para que
al restaurarlo no se pierdan datos que solo viven en la base (tipo, vencimiento, "no vence",
carpeta), esos datos viajan EN EL ARCHIVO, en las propiedades propias de la app (appProperties),
en la misma llamada que lo manda a la papelera.

Restaurar = sacar el archivo de la papelera, ponerlo en la carpeta correcta de su hijo y volver a
crear el registro. Si la persona vacía su papelera de Drive o pasan 30 días, ya no se puede: eso lo
decide Google.

Las funciones terminadas en `_en` reciben el servicio de Drive (se pueden probar sin Google).
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from googleapiclient.errors import HttpError

from .drive_upload import _asegurar_carpeta_hijo, _asegurar_subcarpeta_de_hijo, _drive, _status
from .google_client import traducir_permisos_de_google
from .models import User

logger = logging.getLogger(__name__)

# Google guarda lo que está en la papelera 30 días.
DIAS_EN_PAPELERA = 30
_MAX_PAGINAS = 5

# Marca que identifica a los archivos que son documentos de Docukids.
PROP_DOC = "dk_doc"


class NoRestaurable(Exception):
    """El archivo ya no está en la papelera de Drive, o no es un documento de Docukids."""


def _recortar_bytes(texto: str, maximo: int) -> str:
    """Drive limita clave+valor de cada propiedad a 124 bytes: se recorta por bytes, no por letras."""
    return (texto or "").encode("utf-8")[:maximo].decode("utf-8", errors="ignore")


def propiedades_de_documento(documento) -> Dict[str, str]:
    """Lo que hay que guardar en el archivo para poder reconstruir el documento al restaurarlo."""
    props: Dict[str, str] = {PROP_DOC: "1", "dk_child": str(documento.child_id)}
    if documento.folder_id is not None:
        props["dk_folder"] = str(documento.folder_id)
    nombre = _recortar_bytes(documento.name or "", 100)
    if nombre:
        props["dk_name"] = nombre
    tipo = _recortar_bytes(documento.type or "", 100)
    if tipo:
        props["dk_type"] = tipo
    if documento.expiry_date is not None:
        props["dk_exp"] = documento.expiry_date.isoformat()
    if documento.no_expiry:
        props["dk_noexp"] = "1"
    return props


def mandar_a_papelera_en(drive_service, drive_file_id: str, propiedades: Dict[str, str]) -> bool:
    """
    Manda el archivo a la papelera de Drive y le anota `propiedades`, en UNA sola llamada.
    True = quedó en la papelera. False = el archivo ya no existe (404): no es error.
    Cualquier otro error se propaga: el llamador no borra el registro.
    """
    try:
        drive_service.files().update(
            fileId=drive_file_id,
            body={"trashed": True, "appProperties": propiedades},
            fields="id",
        ).execute()
    except HttpError as error:
        if _status(error) == 404:
            logger.info("Archivo %s ya no existe en Drive; nada que mandar a la papelera.", drive_file_id)
            return False
        logger.warning("No se pudo mandar el archivo %s a la papelera (HTTP %s).", drive_file_id, _status(error))
        raise
    return True


def _dias_restantes(trashed_time: Optional[str], ahora: datetime) -> int:
    """Días que le quedan en la papelera de Drive. Si Google no informa la fecha, se asume lo máximo."""
    if not trashed_time:
        return DIAS_EN_PAPELERA
    try:
        desde = datetime.fromisoformat(trashed_time.replace("Z", "+00:00"))
    except ValueError:
        return DIAS_EN_PAPELERA
    if desde.tzinfo is None:
        desde = desde.replace(tzinfo=timezone.utc)
    return DIAS_EN_PAPELERA - (ahora - desde).days


def listar_papelera_en(drive_service, ahora: Optional[datetime] = None) -> List[Dict]:
    """
    Documentos de Docukids que están en la papelera de Drive porque la persona los borró
    (explicitlyTrashed). Con el permiso drive.file solo se ven los archivos que creó la app.
    Cada elemento: {drive_file_id, name, mime_type, drive_link, days_left, props}. Los que ya
    cumplieron los 30 días no se muestran.
    """
    ahora = ahora or datetime.now(timezone.utc)
    archivos: List[dict] = []
    token = None
    for _ in range(_MAX_PAGINAS):
        respuesta = drive_service.files().list(
            q=f"trashed = true and appProperties has {{ key='{PROP_DOC}' and value='1' }}",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, webViewLink, trashedTime, explicitlyTrashed, appProperties)",
            pageSize=100,
            pageToken=token,
        ).execute()
        archivos.extend(respuesta.get("files", []))
        token = respuesta.get("nextPageToken")
        if not token:
            break

    salida: List[Dict] = []
    for f in archivos:
        if not f.get("explicitlyTrashed", True):
            continue
        dias = _dias_restantes(f.get("trashedTime"), ahora)
        if dias <= 0:
            continue
        salida.append(
            {
                "drive_file_id": f["id"],
                "name": f.get("name", ""),
                "mime_type": f.get("mimeType"),
                "drive_link": f.get("webViewLink"),
                "days_left": dias,
                "props": f.get("appProperties") or {},
            }
        )
    return salida


def leer_de_papelera_en(drive_service, drive_file_id: str) -> Dict:
    """Datos del archivo si está en la papelera y es un documento de Docukids; si no, NoRestaurable."""
    try:
        f = drive_service.files().get(
            fileId=drive_file_id,
            fields="id, name, mimeType, webViewLink, trashed, parents, appProperties",
        ).execute()
    except HttpError as error:
        if _status(error) == 404:
            raise NoRestaurable("El archivo ya no está en Drive.") from error
        raise
    props = f.get("appProperties") or {}
    if not f.get("trashed") or props.get(PROP_DOC) != "1":
        raise NoRestaurable("Ese archivo no está en la papelera como documento de Docukids.")
    return f


def restaurar_en(
    drive_service,
    archivo: Dict,
    hijo_id: int,
    hijo_nombre: str,
    hijo_drive_folder_id: Optional[str],
    nombre_carpeta: Optional[str],
    subcarpeta_guardada: Optional[str],
) -> Dict:
    """
    Saca el archivo de la papelera y lo deja en la carpeta correcta de su hijo (la crea si la
    borraron), en UNA sola llamada: así no queda a medias. Devuelve
    {drive_link, mime_type, hijo_vigente, sub_vigente}: el llamador guarda los ids que cambiaron.
    """
    hijo_vigente = _asegurar_carpeta_hijo(drive_service, hijo_id, hijo_nombre, hijo_drive_folder_id)
    destino = hijo_vigente
    sub_vigente = None
    if nombre_carpeta is not None:
        sub_vigente = _asegurar_subcarpeta_de_hijo(drive_service, hijo_vigente, nombre_carpeta, subcarpeta_guardada)
        destino = sub_vigente

    pedido = {
        "fileId": archivo["id"],
        "body": {"trashed": False},
        "addParents": destino,
        "fields": "id, mimeType, webViewLink",
    }
    quitar = [p for p in (archivo.get("parents") or []) if p != destino]
    if quitar:
        pedido["removeParents"] = ",".join(quitar)
    try:
        actualizado = drive_service.files().update(**pedido).execute()
    except HttpError as error:
        if _status(error) == 404:
            raise NoRestaurable("El archivo ya no está en Drive.") from error
        raise
    return {
        "drive_link": actualizado.get("webViewLink") or archivo.get("webViewLink"),
        "mime_type": actualizado.get("mimeType") or archivo.get("mimeType"),
        "hijo_vigente": hijo_vigente,
        "sub_vigente": sub_vigente,
    }


# ---- Versiones con las credenciales reales del usuario ----

@traducir_permisos_de_google("Google Drive")
def mandar_documento_a_papelera(user: User, drive_file_id: str, propiedades: Dict[str, str]) -> bool:
    return mandar_a_papelera_en(_drive(user), drive_file_id, propiedades)


@traducir_permisos_de_google("Google Drive")
def listar_documentos_en_papelera(user: User) -> List[Dict]:
    return listar_papelera_en(_drive(user))


@traducir_permisos_de_google("Google Drive")
def leer_documento_de_papelera(user: User, drive_file_id: str) -> Dict:
    return leer_de_papelera_en(_drive(user), drive_file_id)


@traducir_permisos_de_google("Google Drive")
def restaurar_documento_en_drive(
    user: User,
    archivo: Dict,
    hijo_id: int,
    hijo_nombre: str,
    hijo_drive_folder_id: Optional[str],
    nombre_carpeta: Optional[str],
    subcarpeta_guardada: Optional[str],
) -> Dict:
    return restaurar_en(
        _drive(user), archivo, hijo_id, hijo_nombre, hijo_drive_folder_id, nombre_carpeta, subcarpeta_guardada
    )
