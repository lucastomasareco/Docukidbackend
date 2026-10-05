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
"""
import io
import logging
from typing import Tuple

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .google_client import build_google_credentials
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


def upload_file_to_drive(user: User, file_bytes: bytes, filename: str, mime_type: str) -> Tuple[str, str]:
    """
    Sube el archivo al Drive del usuario. Devuelve (drive_file_id, drive_link).
    """
    credenciales = build_google_credentials(user, "Google Drive")
    drive_service = build("drive", "v3", credentials=credenciales)
    carpeta_id = _get_or_create_carpeta(drive_service)
    
    metadata = {"name": filename, "parents": [carpeta_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
    
    archivo = (
        drive_service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return archivo["id"], archivo.get("webViewLink", "")


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