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

from sqlalchemy import select, update
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


async def update_child(db: AsyncSession, child: Child, cambios: dict) -> Child:
    """
    Edita un hijo ya validado como propio del usuario (ver get_child_owned_by_user).
    `cambios` trae SOLO los campos que la persona mandó (name y/o birth_date);
    birth_date puede venir como None para borrarla.
    """
    if "name" in cambios:
        child.name = cambios["name"].strip()
    if "birth_date" in cambios:
        child.birth_date = cambios["birth_date"]
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

def compute_status(expiry_date: Optional[date], no_expiry: bool = False) -> str:
    """
    Calcula el status de un documento AL VUELO (no se guarda en la base).
    Reglas exactas de la Guía Técnica, sección 3:
      - vencido: expiry_date < hoy
      - proximo: expiry_date entre hoy y hoy + 30 días
      - vigente: expiry_date > hoy + 30 días
    Si el usuario confirmó que el documento NO vence (no_expiry=True),
    devolvemos "sin_vencimiento" sin mirar la fecha.
    Si todavía no hay fecha de vencimiento (por ejemplo, porque el OCR de la
    Fase 2 aún no corrió), devolvemos "sin_fecha" en vez de inventar un estado.
    """
    if no_expiry:
        return "sin_vencimiento"
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


async def update_document_expiry(
    db: AsyncSession,
    document: Document,
    expiry_date: Optional[date],
    no_expiry: bool,
) -> Document:
    """
    Confirma o corrige el vencimiento de un documento (PATCH /documents/{doc_id}).
    - Con fecha: guarda la fecha y no_expiry pasa a False.
    - Con no_expiry=True: expiry_date pasa a NULL.
    En ambos casos last_notified_at vuelve a NULL, para que una fecha
    corregida pueda volver a disparar el aviso por correo.
    """
    if no_expiry:
        document.expiry_date = None
        document.no_expiry = True
    else:
        document.expiry_date = expiry_date
        document.no_expiry = False
    document.last_notified_at = None
    await db.commit()
    await db.refresh(document)
    return document


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


async def get_appointment_owned_by_user(
    db: AsyncSession, appointment_id: int, user_id: UUID
) -> Optional[Appointment]:
    """
    Igual que get_document_owned_by_user: hace un JOIN contra "children" para
    confirmar que el turno pertenece a un hijo de ESTE usuario.
    """
    result = await db.execute(
        select(Appointment)
        .join(Child, Appointment.child_id == Child.id)
        .where(Appointment.id == appointment_id, Child.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def update_appointment(
    db: AsyncSession,
    appointment: Appointment,
    title: str,
    date_: date,
    time_: Optional[time],
    notes: Optional[str],
    calendar_event_id: str,
) -> Appointment:
    """Guarda los valores FINALES del turno (PATCH /appointments/{id})."""
    appointment.title = title
    appointment.date = date_
    appointment.time = time_
    appointment.notes = notes
    appointment.calendar_event_id = calendar_event_id
    await db.commit()
    await db.refresh(appointment)
    return appointment


async def delete_appointment(db: AsyncSession, appointment: Appointment) -> None:
    await db.delete(appointment)
    await db.commit()


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
            Document.no_expiry.is_(False),
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


async def reclamar_documento_para_aviso(db: AsyncSession, document_id: int) -> bool:
    """
    "Reserva" un documento para mandarle el aviso: pone last_notified_at = hoy
    SOLO SI todavía estaba en NULL, en una única instrucción atómica de la base.
    Devuelve True si lo reservó esta llamada; False si otra corrida del
    scheduler se le adelantó (en cuyo caso NO hay que mandar el correo).
    Es lo que evita correos duplicados cuando cron-job.org reintenta mientras
    la primera llamada todavía está trabajando (típico tras un cold start).
    """
    resultado = await db.execute(
        update(Document)
        .where(Document.id == document_id, Document.last_notified_at.is_(None))
        .values(last_notified_at=hoy())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return resultado.rowcount == 1


async def liberar_documento_de_aviso(db: AsyncSession, document_id: int) -> None:
    """
    Deshace la reserva de reclamar_documento_para_aviso cuando el envío del
    correo FALLÓ, para que el scheduler lo reintente en la próxima corrida.
    """
    await db.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(last_notified_at=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


# ---------- Cuenta ----------

async def delete_user(db: AsyncSession, user: User) -> None:
    """
    Borra al usuario y, en cascada, sus hijos, documentos y turnos (solo en la
    base de datos). NO toca Google (Drive, Calendar) ni Supabase Auth: eso
    lo coordina eliminar_cuenta en main.py.
    """
    await db.delete(user)
    await db.commit()
