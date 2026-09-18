"""
crud.py
-------
CRUD = Create, Read, Update, Delete. Acá vive toda la lógica que habla
directamente con la base de datos. Los endpoints en main.py llaman a estas
funciones en vez de escribir SQL/SQLAlchemy sueltos por todos lados: así, si
mañana hay que cambiar algo de cómo se guarda un documento, se cambia en un
solo lugar.
"""

from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Child, Document, Appointment, User


# ---------- Configuración de zona horaria ----------
# Centralizamos la zona horaria para evitar inconsistencias entre el servidor
# (que suele estar en UTC, como en Render) y los usuarios finales.
# Si mañana el proyecto se expande a México o España, solo se cambia la
# variable de entorno APP_TIMEZONE y todo el sistema se ajusta solo.
TZ = ZoneInfo(settings.app_timezone)


def hoy() -> date:
    """
    Fecha 'hoy' según la zona horaria de los usuarios, no la del servidor.

    Es la ÚNICA fuente de verdad para "hoy" en todo el proyecto. Cualquier
    cálculo de fechas (estado de documentos, umbrales de aviso, marcado de
    notificaciones, días restantes para el correo) debe pasar por acá.
    """
    return datetime.now(TZ).date()


# ---------- Hijos (children) ----------

async def get_children_by_user(db: AsyncSession, user_id: UUID) -> Sequence[Child]:
    result = await db.execute(select(Child).where(Child.user_id == user_id))
    return result.scalars().all()


async def create_child(db: AsyncSession, user_id: UUID, name: str, birth_date: Optional[date]) -> Child:
    child = Child(user_id=user_id, name=name, birth_date=birth_date)
    db.add(child)
    await db.commit()
    await db.refresh(child)
    return child


async def get_child_owned_by_user(db: AsyncSession, child_id: int, user_id: UUID) -> Optional[Child]:
    """
    Busca un hijo por id, pero SOLO si pertenece al usuario que hace el pedido.
    Esto evita que un usuario pueda ver o tocar los hijos de otro usuario
    simplemente adivinando un child_id en la URL.
    """
    result = await db.execute(
        select(Child).where(Child.id == child_id, Child.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def update_child_name(db: AsyncSession, child: Child, name: str) -> Child:
    """Renombra un hijo ya validado como propio del usuario (ver get_child_owned_by_user)."""
    child.name = name
    await db.commit()
    await db.refresh(child)
    return child


async def delete_child(db: AsyncSession, child: Child) -> None:
    """
    Borra el hijo (y, por ON DELETE CASCADE, sus documentos y turnos en la
    base de datos). IMPORTANTE: esto NO borra los archivos en Google Drive ni
    los eventos en Google Calendar -- eso lo tiene que hacer el llamador
    ANTES de invocar esta función (ver borrar_hijo en main.py), porque acá
    solo se toca la base de datos.
    """
    await db.delete(child)
    await db.commit()


# ---------- Documentos ----------

def compute_status(expiry_date: Optional[date]) -> str:
    """
    Calcula el status de un documento AL VUELO (no se guarda en la base).
    Reglas exactas de la Guía Técnica, sección 3:
      - vencido: expiry_date < hoy
      - proximo: expiry_date entre hoy y hoy + 30 días
      - vigente: expiry_date > hoy + 30 días
    Si todavía no hay fecha de vencimiento (por ejemplo, porque el OCR de la
    Fase 2 aún no corrió), devolvemos "sin_fecha" en vez de inventar un estado.
    """
    if expiry_date is None:
        return "sin_fecha"

    # FIX: usamos el helper centralizado en lugar de date.today()
    today = hoy()
    days_left = (expiry_date - today).days

    if days_left < 0:
        return "vencido"
    if days_left <= 30:
        return "proximo"
    return "vigente"


async def create_document(
    db: AsyncSession,
    child_id: int,
    name: str,
    type_: Optional[str],
    drive_file_id: Optional[str] = None,
    drive_link: Optional[str] = None,
    expiry_date: Optional[date] = None,
) -> Document:
    """
    Guarda los metadatos del documento. drive_file_id/drive_link vienen de
    drive_upload.py (Fase 2, paso 2). expiry_date viene de ocr_reader.py
    (Fase 2, paso 3) y puede venir en None si Gemini no encontró una fecha
    de vencimiento clara: el documento queda con status "sin_fecha".
    """
    document = Document(
        child_id=child_id,
        name=name,
        type=type_,
        expiry_date=expiry_date,
        drive_file_id=drive_file_id,
        drive_link=drive_link,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return document


async def get_documents_by_child(db: AsyncSession, child_id: int) -> Sequence[Document]:
    result = await db.execute(select(Document).where(Document.child_id == child_id))
    return result.scalars().all()


async def get_document_owned_by_user(
    db: AsyncSession, doc_id: int, user_id: UUID
) -> Optional[Document]:
    """
    Igual que get_child_owned_by_user, pero para documentos: hace un JOIN
    contra "children" para confirmar que el documento pertenece a un hijo
    de ESTE usuario antes de dejarlo borrar.
    """
    result = await db.execute(
        select(Document)
        .join(Child, Document.child_id == Child.id)
        .where(Document.id == doc_id, Child.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def delete_document(db: AsyncSession, document: Document) -> None:
    await db.delete(document)
    await db.commit()


# ---------- Turnos (appointments) ----------

async def create_appointment(
    db: AsyncSession,
    child_id: int,
    title: str,
    date_: date,
    time_: Optional[time],
    notes: Optional[str],
    calendar_event_id: str,
) -> Appointment:
    appointment = Appointment(
        child_id=child_id,
        title=title,
        date=date_,
        time=time_,
        notes=notes,
        calendar_event_id=calendar_event_id,
    )
    db.add(appointment)
    await db.commit()
    await db.refresh(appointment)
    return appointment


async def get_appointments_by_child(db: AsyncSession, child_id: int) -> Sequence[Appointment]:
    result = await db.execute(select(Appointment).where(Appointment.child_id == child_id))
    return result.scalars().all()


# ---------- Scheduler (avisos de vencimiento) ----------

async def get_documentos_pendientes_de_aviso(db: AsyncSession, umbral_dias: int):
    """
    Trae, en una sola consulta con JOIN, los documentos que:
      - tienen una expiry_date conocida (no "sin_fecha"),
      - todavía NO fueron notificados (last_notified_at es NULL),
      - vencen dentro de "umbral_dias" días (o ya vencieron: no ponemos
        piso inferior, así el usuario se entera igual si un documento
        vencido nunca se le avisó).
    Devuelve tuplas (Document, Child, User) para tener, de una sola vez,
    todo lo que hace falta para armar y mandar el correo (nombre del hijo,
    email del usuario, refresh_token).
    """
    # FIX: usamos el helper centralizado para calcular el límite
    limite = hoy() + timedelta(days=umbral_dias)
    result = await db.execute(
        select(Document, Child, User)
        .join(Child, Document.child_id == Child.id)
        .join(User, Child.user_id == User.id)
        .where(
            Document.expiry_date.isnot(None),
            Document.last_notified_at.is_(None),
            Document.expiry_date <= limite,
        )
    )
    return result.all()


async def marcar_documento_notificado(
    db: AsyncSession, document: Document, fecha: Optional[date] = None
) -> None:
    # FIX: si no viene fecha explícita, usamos el "hoy" de la zona horaria correcta
    document.last_notified_at = fecha or hoy()
    await db.commit()