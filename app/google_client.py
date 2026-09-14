"""
google_client.py
----------------
Módulo compartido con la lógica común para construir credenciales de Google
OAuth a partir del refresh_token guardado en la base de datos.

Esta función vivía duplicada en drive_upload.py, calendar_sync.py y
notifier.py. Ahora vive acá, y los tres módulos la importan. Si mañana hay
que cambiar algo de cómo se renuevan las credenciales (por ejemplo, agregar
reintentos ante fallos transitorios de Google), se cambia en un solo lugar.
"""

from fastapi import HTTPException
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials

from .config import settings
from .models import User

GOOGLE_CLIENT_ID = settings.google_client_id
GOOGLE_CLIENT_SECRET = settings.google_client_secret
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def build_google_credentials(user: User, error_context: str) -> Credentials:
    """
    Construye y renueva credenciales de Google OAuth a partir del
    refresh_token del usuario.

    Parámetro error_context: texto que describe para qué servicio estamos
    pidiendo las credenciales ("Google Drive", "Google Calendar", "Gmail").
    Se usa solo en el mensaje de error 401 para que el usuario sepa cuál
    permiso fue revocado.
    """
    if not user.google_refresh_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "Este usuario todavía no conectó su cuenta de Google. "
                "Primero hay que llamar a GET /auth/google/url y autorizar."
            ),
        )
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise HTTPException(500, "Faltan GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET en el .env")

    credenciales = Credentials(
        token=None,
        refresh_token=user.google_refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
    )
    try:
        credenciales.refresh(GoogleAuthRequest())
    except Exception as error:
        raise HTTPException(
            status_code=401,
            detail=(
                f"No se pudo renovar el acceso a {error_context}: {error}. "
                "Puede que el usuario haya revocado el permiso; repetí /auth/google/url."
            ),
        )
    return credenciales
