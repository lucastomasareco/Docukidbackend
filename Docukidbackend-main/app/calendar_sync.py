"""
calendar_sync.py
-----------------
Fase 2, paso 4 de la Guía Técnica: conecta POST/GET /appointments con
Google Calendar real, usando el mismo google_refresh_token que ya se guardó
en google_oauth.py.

Contrato exacto (Guía Técnica, sección 4):
  POST /appointments {child_id, title, date, time, notes?} -> {ok, calendar_event_id}
  GET  /appointments/{child_id} -> {appointments: [...]}

Nota: la Guía Técnica NO define un DELETE /appointments como endpoint propio,
pero sí hace falta borrar eventos de Calendar cuando se elimina un hijo
completo (ver /children/{child_id} en main.py) -- para eso está
borrar_evento() acá abajo, agregada a pedido explícito del usuario.

--- Refactor hecho ---
La construcción de credenciales OAuth vivía duplicada en drive_upload.py,
calendar_sync.py y notifier.py. Ahora vive en app/google_client.py en la
función build_google_credentials(user, error_context). Los tres módulos la
importan; si hay que tocar la lógica de renovación, se toca en un solo lugar.
"""

from datetime import date as date_type
from datetime import datetime, time as time_type, timedelta
from typing import Optional
import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import settings
from .google_client import build_google_credentials
from .models import User

logger = logging.getLogger(__name__)

# --- Variable NUEVA (no está en la Guía Técnica) ---
# Zona horaria para calcular los eventos. La Guía no la especifica; uso
# Argentina porque los ejemplos del proyecto mencionan Buenos Aires
# (Guía Técnica, sección de Google Cloud Console). Cambiala en el .env si
# el proyecto es para otro país.
TIMEZONE = settings.appointment_timezone

# --- Decisión NUEVA (no está en la Guía Técnica) ---
# El contrato de /appointments no pide una hora de fin, solo date+time. Un
# turno médico típico dura media hora; se puede ajustar acá si hace falta.
DURACION_TURNO_MINUTOS = 30


def _armar_cuerpo_evento(title: str, fecha: date_type, hora: Optional[time_type], notes: Optional[str]) -> dict:
    """
    Si viene una hora, creamos un evento con horario (dateTime + timeZone).
    Si NO viene hora, lo creamos como evento "de todo el día" (date, sin
    horario) -- es lo que hace Google Calendar nativamente cuando un evento
    no tiene hora puntual.
    """
    if hora is not None:
        inicio = datetime.combine(fecha, hora)
        fin = inicio + timedelta(minutes=DURACION_TURNO_MINUTOS)
        return {
            "summary": title,
            "description": notes or "",
            "start": {"dateTime": inicio.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": fin.isoformat(), "timeZone": TIMEZONE},
        }

    return {
        "summary": title,
        "description": notes or "",
        "start": {"date": fecha.isoformat()},
        "end": {"date": fecha.isoformat()},
    }


def crear_evento(
    user: User,
    title: str,
    fecha: date_type,
    hora: Optional[time_type],
    notes: Optional[str],
) -> str:
    """
    Crea el evento en el Google Calendar del usuario (calendario "primary",
    el principal). Devuelve el calendar_event_id para guardarlo en la base.
    """
    credenciales = build_google_credentials(user, "Google Calendar")
    servicio = build("calendar", "v3", credentials=credenciales)

    cuerpo_evento = _armar_cuerpo_evento(title, fecha, hora, notes)

    evento_creado = (
        servicio.events()
        .insert(calendarId="primary", body=cuerpo_evento)
        .execute()
    )
    return evento_creado["id"]


def borrar_evento(user: User, calendar_event_id: str) -> None:
    """
    Borra el evento del Google Calendar del usuario (calendario "primary").
    Si el evento ya no existe (ej. el usuario lo borró a mano desde su
    teléfono), Calendar devuelve 404 o 410 (Gone): en ese caso lo ignoramos,
    igual que hace delete_file_from_drive con Drive. Otros errores se loguean
    y se propagan para no dar por borrado algo que en realidad falló.
    """
    credenciales = build_google_credentials(user, "Google Calendar")
    servicio = build("calendar", "v3", credentials=credenciales)

    try:
        servicio.events().delete(calendarId="primary", eventId=calendar_event_id).execute()
    except HttpError as error:
        status = getattr(error.resp, "status", None)
        if status in (404, 410):
            logger.info(
                "Evento %s ya no existe en Calendar o no tenemos acceso; nada que borrar.",
                calendar_event_id,
            )
            return

        logger.warning("Error al borrar evento %s en Calendar (HTTP %s): %s", calendar_event_id, status, error)
        raise
    except Exception as e:
        logger.exception("Error inesperado al borrar evento %s en Calendar: %s", calendar_event_id, e)
        raise
