"""
account.py
----------
Piezas de "eliminar cuenta" (DELETE /account) que hablan con servicios de
afuera: revocar el permiso de Google y borrar al usuario de Supabase Auth.

Decisiones (cerradas el 8/oct/2026):
  - Los archivos de Drive y los eventos de Calendar NO se borran.
  - Se revoca el permiso que la app tenía sobre la cuenta de Google
    (mejor esfuerzo: si falla, igual se sigue).
  - El usuario se borra de Supabase Auth con la Admin API y la
    SUPABASE_SECRET_KEY (solo backend, nunca en la app).
"""
import logging
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from googleapiclient.errors import HttpError

from .config import settings

logger = logging.getLogger(__name__)

_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def _url_base_supabase() -> str:
    """https://<proyecto>.supabase.co, sacada de SUPABASE_JWKS_URL (así no hace falta otra variable)."""
    partes = urlsplit(settings.supabase_jwks_url)
    return f"{partes.scheme}://{partes.netloc}"


def motivo_de_error(error: Exception) -> str:
    """
    Texto corto y SIN datos privados de por qué falló algo con Google, para el log y para el aviso:
    'Google HTTP 403 insufficientFilePermissions', 'Google HTTP 429 rateLimitExceeded' o, si no es
    un error de la API de Google, solo el tipo ('timeout', 'OSError'...). Nunca incluye tokens.
    """
    if isinstance(error, HttpError):
        estado = getattr(error.resp, "status", "?")
        razon = ""
        try:
            detalles = getattr(error, "error_details", None) or []
            if detalles and isinstance(detalles[0], dict):
                razon = str(detalles[0].get("reason", ""))
        except Exception:
            razon = ""
        return f"Google HTTP {estado} {razon}".strip()
    return type(error).__name__


async def revocar_permiso_de_google(refresh_token: str) -> None:
    """
    Le pide a Google que anule el refresh_token (la app pierde el acceso a
    Drive/Calendar/Gmail de esa cuenta). NUNCA lanza excepción: es un
    mejor esfuerzo y no debe impedir que la cuenta se elimine. No registra
    el token ni la respuesta de Google.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as cliente:
            respuesta = await cliente.post(_GOOGLE_REVOKE_URL, data={"token": refresh_token})
        if respuesta.status_code != 200:
            logger.warning("Google no confirmó la revocación (HTTP %s).", respuesta.status_code)
    except Exception as error:
        logger.warning("No se pudo revocar el permiso de Google (%s).", type(error).__name__)


async def borrar_usuario_de_auth(user_id: UUID) -> None:
    """
    Borra al usuario de Supabase Auth (Admin API). Si ya no existe (404) se
    considera hecho. Cualquier otro problema -> 502, para que la app avise
    y la persona pueda reintentar.
    """
    clave = settings.supabase_secret_key
    cabeceras = {"apikey": clave}
    # Las claves nuevas (sb_secret_...) van solo en "apikey"; las viejas
    # (service_role) son un JWT y además van como Bearer.
    if clave.startswith("eyJ"):
        cabeceras["Authorization"] = f"Bearer {clave}"

    try:
        async with httpx.AsyncClient(timeout=20) as cliente:
            respuesta = await cliente.delete(
                f"{_url_base_supabase()}/auth/v1/admin/users/{user_id}",
                headers=cabeceras,
            )
    except httpx.HTTPError as error:
        logger.error("No se pudo contactar a Supabase Auth (%s).", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo terminar de eliminar la cuenta (no se pudo contactar a Supabase: {type(error).__name__}). Probá de nuevo en un momento.",
        )

    if respuesta.status_code in (200, 204, 404):
        return

    logger.error("Supabase Auth rechazó el borrado del usuario (HTTP %s).", respuesta.status_code)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"No se pudo terminar de eliminar la cuenta (Supabase respondió HTTP {respuesta.status_code}). Probá de nuevo en un momento.",
    )
