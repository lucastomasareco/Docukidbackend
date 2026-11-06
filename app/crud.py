"""
crud.py
-------
CRUD = Create, Read, Update, Delete. Acá vive toda la lógica que habla
directamente con la base de datos. Los endpoints en main.py llaman a estas
funciones en vez de escribir SQL/SQLAlchemy sueltos por todos lados: así, si
mañana hay que cambiar algo de cómo se guarda un documento, se cambia en un
solo lugar.
"""

import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Appointment, Child, ChildFolder, Document, Family, Folder, User


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
    folder_id: Optional[int] = None,
    mime_type: Optional[str] = None,
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
        folder_id=folder_id,
        mime_type=mime_type,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return document


async def set_document_mime_type(db: AsyncSession, document: Document, mime_type: str) -> None:
    """Completa el tipo de un documento anterior a v2.4 (lo informa Drive al pedir la miniatura)."""
    document.mime_type = mime_type
    await db.commit()


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


async def update_document(
    db: AsyncSession,
    document: Document,
    name: Optional[str],
    expiry_date: Optional[date],
    no_expiry: bool,
    cambiar_carpeta: bool = False,
    folder_id: Optional[int] = None,
) -> Document:
    """
    PATCH /documents/{doc_id}: cambia el nombre, el vencimiento y/o la carpeta.
    - cambiar_carpeta=True: el documento pasa a folder_id (None = suelto). No
      toca last_notified_at.
    - Solo el nombre: no toca la fecha ni last_notified_at.
    - Si cambia el vencimiento (fecha nueva o "no vence"), last_notified_at
      vuelve a NULL para que pueda volver a avisar.
    """
    if name is not None:
        document.name = name
    if cambiar_carpeta:
        document.folder_id = folder_id
    if no_expiry:
        document.expiry_date = None
        document.no_expiry = True
        document.last_notified_at = None
    elif expiry_date is not None:
        document.expiry_date = expiry_date
        document.no_expiry = False
        document.last_notified_at = None
    await db.commit()
    await db.refresh(document)
    return document


async def delete_document(db: AsyncSession, document: Document) -> None:
    await db.delete(document)
    await db.commit()


# ---------- Carpetas (folders) ----------

# Carpetas que toda persona tiene desde el principio (se pueden renombrar, no borrar).
CARPETAS_BASE = ("Documentos", "Salud", "Escuela")


async def get_folders_by_user(db: AsyncSession, user_id: UUID) -> Sequence[Folder]:
    """Las de base primero; después las demás por fecha de creación."""
    result = await db.execute(
        select(Folder)
        .where(Folder.user_id == user_id)
        .order_by(Folder.is_base.desc(), Folder.created_at, Folder.id)
    )
    return result.scalars().all()


async def get_folders_ensuring_base(db: AsyncSession, user_id: UUID) -> Sequence[Folder]:
    """
    GET /folders: si la persona no tiene ninguna carpeta, crea las 3 de base
    SOLO en la base de datos (Drive se toca recién al primer uso).
    Si dos pedidos llegan a la vez, el índice único evita duplicados: el que
    pierde la carrera hace rollback y vuelve a leer.
    """
    carpetas = await get_folders_by_user(db, user_id)
    if carpetas:
        return carpetas
    for nombre in CARPETAS_BASE:
        db.add(Folder(user_id=user_id, name=nombre, is_base=True))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
    return await get_folders_by_user(db, user_id)


async def get_folder_owned_by_user(db: AsyncSession, folder_id: int, user_id: UUID) -> Optional[Folder]:
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def folder_name_exists(
    db: AsyncSession, user_id: UUID, name: str, exclude_id: Optional[int] = None
) -> bool:
    """True si la persona ya tiene una carpeta con ese nombre (sin mirar mayúsculas)."""
    consulta = select(Folder.id).where(
        Folder.user_id == user_id, func.lower(Folder.name) == name.lower()
    )
    if exclude_id is not None:
        consulta = consulta.where(Folder.id != exclude_id)
    result = await db.execute(consulta.limit(1))
    return result.scalar_one_or_none() is not None


async def create_folder(
    db: AsyncSession, user_id: UUID, name: str, drive_folder_id: Optional[str] = None
) -> Folder:
    carpeta = Folder(user_id=user_id, name=name, is_base=False, drive_folder_id=drive_folder_id)
    db.add(carpeta)
    await db.commit()  # puede lanzar IntegrityError si justo se repitió el nombre
    await db.refresh(carpeta)
    return carpeta


async def rename_folder(db: AsyncSession, carpeta: Folder, name: str) -> Folder:
    carpeta.name = name
    await db.commit()  # puede lanzar IntegrityError si justo se repitió el nombre
    await db.refresh(carpeta)
    return carpeta


async def set_folder_drive_id(db: AsyncSession, carpeta: Folder, drive_folder_id: Optional[str]) -> None:
    """Guarda el id de Drive de la carpeta (se crea al primer uso o se recrea si la borraron)."""
    if carpeta.drive_folder_id == drive_folder_id:
        return
    carpeta.drive_folder_id = drive_folder_id
    await db.commit()
    await db.refresh(carpeta)


# ---------- Drive por hijo (v2.6, Guía §13) ----------

async def set_child_drive_folder_id(db: AsyncSession, hijo: Child, drive_folder_id: Optional[str]) -> None:
    """Guarda el id de la carpeta del hijo en Drive (se crea al primer uso o se recrea si la borraron)."""
    if hijo.drive_folder_id == drive_folder_id:
        return
    hijo.drive_folder_id = drive_folder_id
    await db.commit()
    await db.refresh(hijo)


async def get_linked_child_drive_folder_ids(db: AsyncSession, user_id: UUID) -> list:
    """Ids de Drive de las carpetas de hijo ya vinculadas a algún hijo de este usuario."""
    result = await db.execute(
        select(Child.drive_folder_id).where(
            Child.user_id == user_id, Child.drive_folder_id.is_not(None)
        )
    )
    return [r for r in result.scalars().all()]


async def get_child_folder_drive_id(db: AsyncSession, child_id: int, folder_id: int) -> Optional[str]:
    """Id en Drive de la subcarpeta `folder_id` DE ESTE HIJO (None = todavía no creada)."""
    result = await db.execute(
        select(ChildFolder.drive_folder_id).where(
            ChildFolder.child_id == child_id, ChildFolder.folder_id == folder_id
        )
    )
    return result.scalar_one_or_none()


async def get_child_folders_of_folder(db: AsyncSession, folder_id: int) -> Sequence[ChildFolder]:
    """Las subcarpetas de Drive (una por hijo) que tiene una carpeta de la app."""
    result = await db.execute(select(ChildFolder).where(ChildFolder.folder_id == folder_id))
    return result.scalars().all()


async def get_child_folder_drive_ids(db: AsyncSession, child_id: int) -> list:
    """Ids en Drive de TODAS las subcarpetas de este hijo (para limpiarlas al borrarlo)."""
    result = await db.execute(
        select(ChildFolder.drive_folder_id).where(ChildFolder.child_id == child_id)
    )
    return list(result.scalars().all())


async def set_child_folder_drive_id(
    db: AsyncSession, child_id: int, folder_id: int, drive_folder_id: str
) -> None:
    """Guarda (o actualiza) el id en Drive de la subcarpeta de este hijo."""
    result = await db.execute(
        select(ChildFolder).where(
            ChildFolder.child_id == child_id, ChildFolder.folder_id == folder_id
        )
    )
    fila = result.scalar_one_or_none()
    if fila is None:
        db.add(ChildFolder(child_id=child_id, folder_id=folder_id, drive_folder_id=drive_folder_id))
    elif fila.drive_folder_id == drive_folder_id:
        return
    else:
        fila.drive_folder_id = drive_folder_id
    await db.commit()


async def get_documents_to_migrate(db: AsyncSession, user_id: UUID, limite: int) -> list:
    """
    Migración de Drive (paso 6): los próximos `limite` documentos de este usuario que
    tienen archivo en Drive y todavía no se migraron, los de id más bajo primero.
    Devuelve filas (Document, Child) para tener a mano los datos del hijo.
    """
    result = await db.execute(
        select(Document, Child)
        .join(Child, Document.child_id == Child.id)
        .where(
            Child.user_id == user_id,
            Document.drive_file_id.is_not(None),
            Document.drive_migrated.is_(False),
        )
        .order_by(Document.id)
        .limit(limite)
    )
    return list(result.all())


async def count_documents_to_migrate(db: AsyncSession, user_id: UUID) -> int:
    """Cuántos documentos de este usuario faltan migrar (el `remaining` de POST /drive/migrate)."""
    result = await db.execute(
        select(func.count())
        .select_from(Document)
        .join(Child, Document.child_id == Child.id)
        .where(
            Child.user_id == user_id,
            Document.drive_file_id.is_not(None),
            Document.drive_migrated.is_(False),
        )
    )
    return int(result.scalar_one())


async def mark_document_migrated(db: AsyncSession, document: Document) -> None:
    """Marca un documento como migrado (ya está en su lugar, o el archivo ya no existe)."""
    if document.drive_migrated:
        return
    document.drive_migrated = True
    await db.commit()


async def get_documents_in_folder(db: AsyncSession, folder_id: int) -> Sequence[Document]:
    result = await db.execute(select(Document).where(Document.folder_id == folder_id))
    return result.scalars().all()


async def delete_folder(db: AsyncSession, carpeta: Folder) -> None:
    """
    Borra la fila. Sus documentos quedan sueltos (ON DELETE SET NULL en la base).
    Los archivos en Drive los mueve ANTES el llamador (ver borrar_carpeta en main.py).
    """
    await db.delete(carpeta)
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


# ---------- Familias (v2.5) ----------

# Letras y números SIN los que se confunden al leerlos o dictarlos (0/O, 1/I/L).
_ALFABETO_CODIGO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODIGO_LARGO = 6
CODIGO_HORAS_DE_VALIDEZ = 24
MAX_FAMILIA = 2  # titular + un miembro


def ahora_utc() -> datetime:
    """
    Hora actual en UTC y SIN zona horaria. Las columnas TIMESTAMP de la base
    (families.invite_expires_at) no guardan zona, así que SIEMPRE escribimos
    y comparamos en UTC. (No confundir con hoy(), que es una FECHA en la zona
    de los usuarios.)
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generar_codigo_de_invitacion() -> str:
    """Código al azar de 6 caracteres. Usa `secrets` (seguro), no `random`."""
    return "".join(secrets.choice(_ALFABETO_CODIGO) for _ in range(CODIGO_LARGO))


def invitacion_vigente(familia: Family) -> bool:
    """True si la familia tiene un código que todavía no venció."""
    return (
        bool(familia.invite_code)
        and familia.invite_expires_at is not None
        and familia.invite_expires_at > ahora_utc()
    )


async def get_family(db: AsyncSession, family_id: int) -> Optional[Family]:
    return await db.get(Family, family_id)


async def get_family_members(db: AsyncSession, family_id: int) -> Sequence[User]:
    """Todas las personas de la familia (el titular también tiene family_id)."""
    result = await db.execute(
        select(User).where(User.family_id == family_id).order_by(User.created_at)
    )
    return result.scalars().all()


async def create_family(db: AsyncSession, usuario: User) -> Optional[Family]:
    """
    Crea la familia con `usuario` como titular y lo anota en users.family_id,
    todo en una sola operación (o se hace todo o no se hace nada).
    Devuelve None si la base la rechazó porque esa persona YA es titular de una
    familia (families.owner_id es UNIQUE): el llamador responde 409.
    """
    familia = Family(owner_id=usuario.id)
    db.add(familia)
    try:
        await db.flush()  # la base le asigna el id a la familia, todavía sin confirmar
    except IntegrityError:
        await db.rollback()
        return None
    usuario.family_id = familia.id
    await db.commit()
    await db.refresh(familia)
    return familia


async def create_invite(db: AsyncSession, family_id: int) -> tuple[str, datetime]:
    """
    Genera un código nuevo para la familia y REEMPLAZA al anterior (si había).
    Devuelve (codigo, vence_en_utc_sin_zona). Si el código al azar justo
    coincide con el de otra familia (invite_code es UNIQUE), prueba con otro.
    No escribe el código en ningún log.
    """
    for _ in range(5):
        codigo = generar_codigo_de_invitacion()
        vence = ahora_utc() + timedelta(hours=CODIGO_HORAS_DE_VALIDEZ)
        try:
            await db.execute(
                update(Family)
                .where(Family.id == family_id)
                .values(invite_code=codigo, invite_expires_at=vence)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            return codigo, vence
        except IntegrityError:
            await db.rollback()
    raise RuntimeError("No se pudo generar un código de invitación único")


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> Optional[User]:
    return await db.get(User, user_id)


async def get_family_by_active_code(db: AsyncSession, codigo: str) -> Optional[Family]:
    """
    La familia que tiene ESE código, solo si todavía no venció. Código
    inexistente, vencido o ya usado (se borra al usarse) = None: a propósito
    no se distingue entre ellos.
    """
    result = await db.execute(
        select(Family).where(
            Family.invite_code == codigo,
            Family.invite_expires_at > ahora_utc(),
        )
    )
    return result.scalar_one_or_none()


async def user_has_own_data(db: AsyncSession, user_id: UUID) -> bool:
    """
    True si la persona ya tiene hijos propios. Los documentos y turnos cuelgan
    de los hijos, así que mirar hijos alcanza. Las carpetas NO cuentan: las 3
    de base se crean solas la primera vez que se pide /folders.
    """
    result = await db.execute(select(Child.id).where(Child.user_id == user_id).limit(1))
    return result.first() is not None


async def join_family(db: AsyncSession, usuario: User, family_id: int, codigo: str) -> bool:
    """
    Mete a `usuario` en la familia usando el código, en UNA operación atómica:
      1) "gasta" el código (lo pone en NULL) solo si sigue siendo ese y no venció.
         Si dos personas lo usan a la vez, solo a una le sale rowcount == 1.
      2) anota family_id en la persona y le borra cualquier dato de Google propio
         (el miembro nunca tiene google_refresh_token: usa el del titular).
    Devuelve False (sin cambiar nada) si el código ya no servía.
    """
    resultado = await db.execute(
        update(Family)
        .where(
            Family.id == family_id,
            Family.invite_code == codigo,
            Family.invite_expires_at > ahora_utc(),
        )
        .values(invite_code=None, invite_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    if resultado.rowcount != 1:
        await db.rollback()
        return False
    usuario.family_id = family_id
    usuario.google_refresh_token = None
    usuario.google_email = None
    await db.commit()
    return True


async def remove_from_family(db: AsyncSession, usuario: User) -> None:
    """Saca a una persona de su familia (ella se va, o el titular la quita). No se copia ni se borra ningún dato."""
    usuario.family_id = None
    await db.commit()


async def dissolve_family(db: AsyncSession, family_id: int) -> None:
    """
    Disuelve la familia: todas las personas (titular incluido) quedan sin
    familia y se borra la fila de `families`. Los datos del titular no se tocan.
    """
    await db.execute(
        update(User)
        .where(User.family_id == family_id)
        .values(family_id=None)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(Family).where(Family.id == family_id).execution_options(synchronize_session=False)
    )
    await db.commit()
