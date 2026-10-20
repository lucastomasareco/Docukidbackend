"""
drive_upload.py
Fase 2, paso 2 de la Guía Técnica: conecta /upload a la subida real en
Google Drive (hasta ahora era "simulado": solo guardaba metadatos).
Idea central (Guía Técnica, sección 1 y 4): el backend sube el archivo a la
cuenta de Google Drive del USUARIO, no a un Drive propio del proyecto. Para
eso usa el google_refresh_token que se guardó en google_oauth.py.
--- Decisión de privacidad ---
El archivo queda con los permisos por defecto de Drive: SOLO el dueño de esa
cuenta de Google puede verlo. NO lo hacemos público ni "cualquiera con el link".
--- Decisión de organización ---
Los archivos se guardan dentro de una carpeta "Docukids" en el Drive del
usuario (se crea sola la primera vez), en vez de tirarlos sueltos en la raíz.
--- Carpetas (Guía v2.3, estructura A) ---
Las carpetas de la app (Salud, Escuela, ...) son SUBCARPETAS de "Docukids":
    Docukids / Salud / archivo
Un documento sin carpeta queda directo en "Docukids". Si la persona borra una
subcarpeta a mano en Drive, asegurar_subcarpeta la vuelve a crear.
"""
import io
import logging
import re
from typing import List, Optional, Tuple

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .google_client import build_google_credentials, traducir_permisos_de_google
from .models import User

logger = logging.getLogger(__name__)

DRIVE_FOLDER_NAME = "Docukids"


def _get_or_create_carpeta(drive_service) -> str:
    """Busca la carpeta 'Docukids' en el Drive del usuario; si no existe, la crea."""
    query = (
        f"name='{DRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' "
        "and trashed=false"
    )
    resultado = drive_service.files().list(
        q=query, spaces="drive", fields="files(id, name)"
    ).execute()
    
    encontradas = resultado.get("files", [])
    if encontradas:
        return encontradas[0]["id"]
    
    metadata = {
        "name": DRIVE_FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder"
    }
    carpeta = drive_service.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


_MIME_CARPETA = "application/vnd.google-apps.folder"


def _drive(user: User):
    """Arma el cliente de Drive con las credenciales del usuario."""
    credenciales = build_google_credentials(user, "Google Drive")
    return build("drive", "v3", credentials=credenciales)


def _status(error: HttpError):
    return getattr(error.resp, "status", None)


def _subcarpeta_vive(drive_service, drive_folder_id: str) -> bool:
    """True si la carpeta existe en Drive y no está en la papelera."""
    try:
        carpeta = drive_service.files().get(fileId=drive_folder_id, fields="id, trashed").execute()
    except HttpError as error:
        if _status(error) == 404:
            return False
        raise
    return not carpeta.get("trashed", False)


def _crear_subcarpeta(drive_service, nombre: str) -> str:
    """Crea la carpeta `nombre` dentro de 'Docukids' y devuelve su id."""
    raiz_id = _get_or_create_carpeta(drive_service)
    metadata = {"name": nombre, "mimeType": _MIME_CARPETA, "parents": [raiz_id]}
    carpeta = drive_service.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


def _asegurar_subcarpeta(drive_service, nombre: str, drive_folder_id: Optional[str]) -> str:
    """Devuelve el id de una subcarpeta que EXISTE: la guardada, o una nueva si falta."""
    if drive_folder_id and _subcarpeta_vive(drive_service, drive_folder_id):
        return drive_folder_id
    return _crear_subcarpeta(drive_service, nombre)


def _subir(drive_service, carpeta_id: str, file_bytes: bytes, filename: str, mime_type: str) -> Tuple[str, str]:
    metadata = {"name": filename, "parents": [carpeta_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
    archivo = (
        drive_service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return archivo["id"], archivo.get("webViewLink", "")


@traducir_permisos_de_google("Google Drive")
def upload_file_to_drive(user: User, file_bytes: bytes, filename: str, mime_type: str) -> Tuple[str, str]:
    """
    Sube el archivo a la raíz 'Docukids' del Drive del usuario.
    Devuelve (drive_file_id, drive_link).
    """
    drive_service = _drive(user)
    carpeta_id = _get_or_create_carpeta(drive_service)
    return _subir(drive_service, carpeta_id, file_bytes, filename, mime_type)


@traducir_permisos_de_google("Google Drive")
def upload_file_to_drive_folder(
    user: User,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    folder_name: str,
    drive_folder_id: Optional[str],
) -> Tuple[str, str, str]:
    """
    Sube el archivo a la subcarpeta `folder_name` de 'Docukids' (la crea si no
    existe todavía o si la borraron a mano).
    Devuelve (drive_file_id, drive_link, drive_folder_id_vigente): el llamador
    debe guardar ese último id si cambió.
    """
    drive_service = _drive(user)
    carpeta_id = _asegurar_subcarpeta(drive_service, folder_name, drive_folder_id)
    archivo_id, link = _subir(drive_service, carpeta_id, file_bytes, filename, mime_type)
    return archivo_id, link, carpeta_id


@traducir_permisos_de_google("Google Drive")
def crear_carpeta_en_drive(user: User, folder_name: str) -> str:
    """Crea la subcarpeta en Drive y devuelve su id (POST /folders)."""
    return _crear_subcarpeta(_drive(user), folder_name)


@traducir_permisos_de_google("Google Drive")
def renombrar_carpeta_en_drive(user: User, drive_folder_id: str, nuevo_nombre: str) -> None:
    """
    Renombra la subcarpeta en Drive. Si ya no existe (404), no hay nada que
    renombrar: se crea de nuevo, con el nombre correcto, la próxima vez que se use.
    """
    drive_service = _drive(user)
    try:
        drive_service.files().update(fileId=drive_folder_id, body={"name": nuevo_nombre}).execute()
    except HttpError as error:
        if _status(error) == 404:
            logger.info("Carpeta %s ya no existe en Drive; nada que renombrar.", drive_folder_id)
            return
        raise


def _mover(drive_service, drive_file_id: str, destino_id: str) -> None:
    """Mueve un archivo a la carpeta `destino_id` (la saca de donde estuviera)."""
    archivo = drive_service.files().get(fileId=drive_file_id, fields="id, parents").execute()
    padres_actuales = archivo.get("parents", [])
    if padres_actuales == [destino_id]:
        return
    drive_service.files().update(
        fileId=drive_file_id,
        addParents=destino_id,
        removeParents=",".join(p for p in padres_actuales if p != destino_id),
        fields="id",
    ).execute()


@traducir_permisos_de_google("Google Drive")
def mover_archivo_a_carpeta(
    user: User,
    drive_file_id: str,
    folder_name: Optional[str],
    drive_folder_id: Optional[str],
) -> Optional[str]:
    """
    Mueve el archivo a la subcarpeta `folder_name`, o a la raíz 'Docukids' si
    folder_name es None. Devuelve el id de la subcarpeta vigente (None si fue a
    la raíz); el llamador lo guarda si cambió. Si el archivo ya no existe en
    Drive (404), no se hace nada.
    """
    drive_service = _drive(user)
    try:
        if folder_name is None:
            _mover(drive_service, drive_file_id, _get_or_create_carpeta(drive_service))
            return None
        carpeta_id = _asegurar_subcarpeta(drive_service, folder_name, drive_folder_id)
        _mover(drive_service, drive_file_id, carpeta_id)
        return carpeta_id
    except HttpError as error:
        if _status(error) == 404:
            logger.info("Archivo %s ya no existe en Drive; no se mueve.", drive_file_id)
            return drive_folder_id if folder_name is not None else None
        raise


@traducir_permisos_de_google("Google Drive")
def borrar_carpeta_de_drive(user: User, drive_folder_id: str, drive_file_ids: List[str]) -> None:
    """
    Borra una subcarpeta SIN perder archivos: primero mueve a la raíz 'Docukids'
    los archivos que se le pasan, y recién después borra la carpeta.
    Un 404 (archivo o carpeta que ya no existe) se ignora; cualquier otro error
    se propaga para que el llamador NO borre nada de la base.
    """
    drive_service = _drive(user)
    if drive_file_ids:
        raiz_id = _get_or_create_carpeta(drive_service)
        for archivo_id in drive_file_ids:
            try:
                _mover(drive_service, archivo_id, raiz_id)
            except HttpError as error:
                if _status(error) == 404:
                    continue
                raise
    try:
        drive_service.files().delete(fileId=drive_folder_id).execute()
    except HttpError as error:
        if _status(error) == 404:
            return
        raise


@traducir_permisos_de_google("Google Drive")
def delete_file_from_drive(user: User, drive_file_id: str) -> None:
    """
    Borra el archivo del Drive del usuario. 
    Si el archivo ya no existe (ej. el usuario lo borró a mano), se ignora (404).
    Otros errores (red, 500, rate limit, etc.) se loguean y se propagan para 
    evitar dejar archivos huérfanos en caso de fallos transitorios.
    """
    credenciales = build_google_credentials(user, "Google Drive")
    drive_service = build("drive", "v3", credentials=credenciales)

    try:
        drive_service.files().delete(fileId=drive_file_id).execute()
    except HttpError as error:
        status = getattr(error.resp, "status", None)
        if status == 404:
            logger.info("Archivo %s ya no existe en Drive o no tenemos acceso; nada que borrar.", drive_file_id)
            return
        
        # Para cualquier otro error HTTP (403, 500, 503, etc.), logueamos y propagamos
        logger.warning("Error al borrar archivo %s en Drive (HTTP %s): %s", drive_file_id, status, error)
        raise
    except Exception as e:
        # Para errores no HTTP (ej. problemas de red locales, timeout, etc.)
        logger.exception("Error inesperado al borrar archivo %s en Drive: %s", drive_file_id, e)
        raise

# ---------------------------------------------------------------------------
# Miniaturas (v2.4): GET /documents/{doc_id}/thumbnail
# ---------------------------------------------------------------------------
# Drive genera miniaturas de fotos y de PDF (primera página). El enlace
# ("thumbnailLink") dura pocas horas y, si el archivo no es público, hay que
# pedirlo con las credenciales de la persona. Por eso la imagen la baja el
# backend y la reenvía a la app: la app nunca ve enlaces ni tokens de Google.

PX_MINIATURA = {"small": 240, "large": 1600}
_TIMEOUT_MINIATURA = 10  # segundos de espera con Google


class SinMiniatura(Exception):
    """Drive no tiene miniatura para este archivo (todavía) o el archivo ya no está."""

    def __init__(self, mime_type: Optional[str] = None):
        super().__init__("Sin miniatura")
        # Si Drive llegó a decirnos el tipo del archivo, lo conservamos: sirve igual.
        self.mime_type = mime_type


def _con_tamano(enlace: str, px: int) -> str:
    """El enlace de Drive termina en '=s220' (tamaño en píxeles); lo cambiamos por el pedido."""
    if re.search(r"=s\d+(-[a-z]+)*$", enlace):
        return re.sub(r"=s\d+(-[a-z]+)*$", f"=s{px}", enlace)
    return enlace  # formato inesperado: se usa tal cual (Drive elegirá el tamaño)


@traducir_permisos_de_google("Google Drive")
def obtener_miniatura_de_drive(user: User, drive_file_id: str, px: int) -> Tuple[bytes, str, Optional[str]]:
    """
    Devuelve (bytes_de_la_imagen, content_type, mime_type_del_archivo_original).

    Lanza SinMiniatura si el archivo no existe, está en la papelera o Drive todavía
    no tiene miniatura. Otros errores (red, 429, 5xx, respuesta rara) se propagan
    y main.py los traduce a 502. No se registran enlaces ni tokens en los logs.
    """
    credenciales = build_google_credentials(user, "Google Drive")
    drive_service = build("drive", "v3", credentials=credenciales)

    try:
        info = drive_service.files().get(
            fileId=drive_file_id, fields="mimeType, thumbnailLink, trashed"
        ).execute()
    except HttpError as error:
        if _status(error) == 404:
            raise SinMiniatura()
        raise

    mime = info.get("mimeType")
    enlace = info.get("thumbnailLink")
    if info.get("trashed") or not enlace:
        raise SinMiniatura(mime)

    sesion = AuthorizedSession(credenciales)
    try:
        respuesta = sesion.get(_con_tamano(enlace, px), timeout=_TIMEOUT_MINIATURA)
    finally:
        sesion.close()

    if respuesta.status_code in (403, 404):
        # El enlace venció o Drive todavía no terminó de generarla.
        raise SinMiniatura(mime)
    respuesta.raise_for_status()

    tipo = (respuesta.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not tipo.startswith("image/") or not respuesta.content:
        raise SinMiniatura(mime)
    return respuesta.content, tipo, mime
