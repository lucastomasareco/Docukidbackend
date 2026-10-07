"""
main.py
Este es el archivo que arrancas para levantar el servidor. Registra los
endpoints de la Fase 1 del plan de construcción (Guía Técnica, sección 7):
CRUD básico, SIN integraciones externas todavía (nada de Google Drive,
Gemini, Google Calendar ni correos: eso es la Fase 2).
Endpoints que vas a encontrar acá (ver contrato completo en Guía Técnica,
sección 4):
GET    /                       -> chequeo simple de que el server está vivo
GET    /auth/google/url        -> genera la URL de autorización de Google (Fase 2, paso 1)
GET    /auth/google/callback   -> recibe la respuesta de Google y guarda el refresh_token (Fase 2, paso 1)
GET    /children               -> lista los hijos del usuario logueado
POST   /children               -> crea un hijo nuevo
PUT    /children/{child_id}    -> renombra un hijo
DELETE /children/{child_id}    -> borra un hijo entero (documentos en Drive, turnos en Calendar y metadatos)
POST   /upload                 -> "sube" un documento (todavía simulado)
GET    /documents/{child_id}   -> lista documentos de un hijo, con status calculado
DELETE /documents/{doc_id}     -> borra un documento
POST   /appointments           -> crea un turno médico (evento real en Google Calendar)
GET    /appointments/{child_id} -> lista los turnos de un hijo
POST   /scheduler/check         -> lo llama cron-job.org 1 vez al día; manda los avisos pendientes
/upload ahora hace, en este orden: valida el hijo y el tipo de archivo,
sube el archivo a Drive (drive_upload.py), y le pasa el archivo a Gemini
para detectar la fecha de vencimiento (ocr_reader.py).
Con este archivo se completa la Fase 2 completa de la Guía Técnica.
Lo que sigue (Fase 3) es configurar cron-job.org, desplegar en Render, y
probar todo de punta a punta -- ya no es código nuevo del backend.
"""
import re
from typing import Optional
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from . import crud
from .auth import get_current_user
from .calendar_sync import crear_evento, borrar_evento
from .database import get_db
from .drive_upload import delete_file_from_drive, upload_file_to_drive
from .google_oauth import router as google_oauth_router
from .models import User
from .ocr_reader import leer_fecha_de_vencimiento
from .scheduler import router as scheduler_router
from .schemas import (
    AppointmentCreate,
    AppointmentCreatedResponse,
    AppointmentOut,
    AppointmentsResponse,
    ChildCreate,
    ChildOut,
    ChildResponse,
    ChildrenResponse,
    ChildUpdate,
    DeleteResponse,
    DocumentOut,
    DocumentsResponse,
    UploadResponse,
)

app = FastAPI(title="Docukids API", version="0.1.0 (Fase 1 - backend base)")

# La app móvil no corre en un navegador con las mismas reglas que una web,
# pero dejamos CORS abierto para no tener sorpresas si en algún momento se
# prueba la API desde una herramienta web (Swagger UI incluido) o desde un
# emulador. No expone nada que ya no esté protegido por el token JWT.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(google_oauth_router)
app.include_router(scheduler_router)

@app.get("/")
async def raiz():
    """Endpoint simple para confirmar que el servidor está corriendo."""
    return {"mensaje": "Docukids API - Fase 1 (backend base) funcionando"}

# ---------- /children ----------
@app.get("/children", response_model=ChildrenResponse)
async def listar_hijos(
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    hijos = await crud.get_children_by_user(db, usuario.id)
    return ChildrenResponse(children=[ChildOut.model_validate(h) for h in hijos])

@app.post("/children", response_model=ChildResponse)
async def crear_hijo(
    datos: ChildCreate,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    hijo = await crud.create_child(db, usuario.id, datos.name, datos.birth_date)
    return ChildResponse(child=ChildOut.model_validate(hijo))

@app.put("/children/{child_id}", response_model=ChildResponse)
async def editar_hijo(
    child_id: int,
    datos: ChildUpdate,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )
    hijo = await crud.update_child_name(db, hijo, datos.name)
    return ChildResponse(child=ChildOut.model_validate(hijo))

@app.delete("/children/{child_id}", response_model=DeleteResponse)
async def borrar_hijo(
    child_id: int,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Borra un hijo completo: sus documentos en Drive, sus turnos en Calendar,
    y sus metadatos en la base (el ON DELETE CASCADE de la tabla se encarga
    de documents/appointments una vez que se borra la fila de children).

    Igual que en borrar_documento, el borrado en Google es "best-effort" en
    el sentido de que un 404 (ya no existe) se ignora, pero cualquier otro
    error de Drive/Calendar corta acá y NO se borra nada de la base -- así
    evitamos que la app "muestre" que el hijo se borró cuando en realidad
    quedaron archivos o turnos huérfanos en la cuenta de Google del usuario.
    """
    hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )

    documentos = await crud.get_documents_by_child(db, child_id)
    for documento in documentos:
        if documento.drive_file_id:
            await run_in_threadpool(delete_file_from_drive, usuario, documento.drive_file_id)

    turnos = await crud.get_appointments_by_child(db, child_id)
    for turno in turnos:
        if turno.calendar_event_id:
            await run_in_threadpool(borrar_evento, usuario, turno.calendar_event_id)

    await crud.delete_child(db, hijo)
    return DeleteResponse(message="Deleted")

# ---------- /upload ----------
# Tipos de archivo que aceptamos. La Guía Técnica habla de "foto o PDF"
# (Docukids.md, sección 3-A), así que validamos eso explícitamente en vez de
# aceptar cualquier cosa (evita, por ejemplo, que alguien suba un .exe).
_TIPOS_PERMITIDOS = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"}

# --- Mejoras de seguridad para la subida de archivos (Fase 2) ---
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_CHUNK_SIZE = 1024 * 1024      # 1 MB por lectura

async def _leer_con_limite(file: UploadFile, max_bytes: int) -> bytes:
    """Lee el archivo en chunks para evitar agotar la memoria RAM (protección contra DoS)."""
    buffer = bytearray()
    while chunk := await file.read(_CHUNK_SIZE):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"El archivo supera el límite de {max_bytes // (1024*1024)} MB",
            )
    return bytes(buffer)

def _sniff_mime(data: bytes) -> Optional[str]:
    """Detecta el tipo real del archivo por sus 'magic numbers' (firma binaria)."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # HEIC/HEIF suele tener 'ftyp' en el offset 4, seguido de la marca específica
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    return None

def _safe_filename(s: str) -> str:
    """Sanitiza el nombre del archivo para evitar caracteres peligrosos o rutas relativas."""
    s = re.sub(r"[^\w\s\-.]", "_", s, flags=re.UNICODE)
    return s.strip()[:200] or "documento"

@app.post("/upload", response_model=UploadResponse)
async def subir_documento(
    file: UploadFile = File(...),
    child_id: int = Form(...),
    name: str = Form(...),
    type: Optional[str] = Form(None),
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Primero confirmamos que el hijo exista Y sea del usuario que llama.
    hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )
    
    # Leemos el archivo en chunks para evitar agotar la memoria RAM (protección contra DoS)
    contenido = await _leer_con_limite(file, _MAX_BYTES)
    if len(contenido) == 0:
        raise HTTPException(status_code=400, detail="El archivo llegó vacío")
    
    # Validamos el tipo de archivo por su firma real (magic numbers) en lugar de confiar solo en el Content-Type
    mime_real = _sniff_mime(contenido)
    if mime_real is None:
        raise HTTPException(
            status_code=400,
            detail="El contenido del archivo no coincide con un PDF o una imagen válida.",
        )
    
    # Opcional: validamos que el MIME real detectado esté en nuestra lista blanca
    if mime_real not in _TIPOS_PERMITIDOS:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de archivo no permitido ({mime_real}). Subí una foto o un PDF.",
        )
    
    # Usamos el MIME real detectado, no el que envió el cliente (evita falsos positivos con HEIC, etc.)
    mime_type = mime_real

    # --- Fase 2, paso 2: subida real a Drive ---
    # Si el usuario todavía no conectó su cuenta de Google, upload_file_to_drive
    # levanta un HTTPException 400 clara pidiendo pasar primero por
    # GET /auth/google/url (ver drive_upload.py).
    nombre_archivo = f"{_safe_filename(hijo.name)} - {_safe_filename(name)}"
    drive_file_id, drive_link = await run_in_threadpool(
        upload_file_to_drive,
        user=usuario,
        file_bytes=contenido,
        filename=nombre_archivo,
        mime_type=mime_type,
    )
    
    # --- Fase 2, paso 3: OCR con Gemini para detectar la fecha de vencimiento ---
    # Nunca lanza excepción (ver ocr_reader.py): si falla, expiry_date queda
    # en None y el documento se guarda igual, con status "sin_fecha".
    fecha_vencimiento = await run_in_threadpool(
        leer_fecha_de_vencimiento, contenido, mime_type
    )
    
    documento = await crud.create_document(
        db,
        child_id=child_id,
        name=name,
        type_=type,
        drive_file_id=drive_file_id,
        drive_link=drive_link,
        expiry_date=fecha_vencimiento,
    )
    
    return UploadResponse(
        status=crud.compute_status(documento.expiry_date),
        expiry_date=documento.expiry_date,
        drive_link=documento.drive_link,
        doc_id=documento.id,
    )

# ---------- /documents ----------
@app.get("/documents/{child_id}", response_model=DocumentsResponse)
async def listar_documentos(
    child_id: int,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )
    
    documentos = await crud.get_documents_by_child(db, child_id)
    salida = [
        DocumentOut(
            id=documento.id,
            name=documento.name,
            expiry_date=documento.expiry_date,
            status=crud.compute_status(documento.expiry_date),
            drive_link=documento.drive_link,
        )
        for documento in documentos
    ]
    return DocumentsResponse(documents=salida)

@app.delete("/documents/{doc_id}", response_model=DeleteResponse)
async def borrar_documento(
    doc_id: int,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    documento = await crud.get_document_owned_by_user(db, doc_id, usuario.id)
    if documento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese doc_id no existe o no pertenece a tu usuario",
        )
    
    # Borramos primero de Drive (best-effort: si falla, igual seguimos y
    # borramos el registro de la base -- ver nota en drive_upload.py).
    if documento.drive_file_id:
        await run_in_threadpool(delete_file_from_drive, usuario, documento.drive_file_id)
    await crud.delete_document(db, documento)
    return DeleteResponse(message="Deleted")

# ---------- /appointments ----------
@app.post("/appointments", response_model=AppointmentCreatedResponse)
async def crear_turno(
    datos: AppointmentCreate,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    hijo = await crud.get_child_owned_by_user(db, datos.child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )
    
    # --- Fase 2, paso 4: evento real en Google Calendar ---
    # Si el usuario todavía no conectó su cuenta de Google, crear_evento
    # levanta un HTTPException 400 clara (ver calendar_sync.py).
    calendar_event_id = await run_in_threadpool(
        crear_evento,
        user=usuario,
        title=datos.title,
        fecha=datos.date,
        hora=datos.time,
        notes=datos.notes,
    )
    await crud.create_appointment(
        db,
        child_id=datos.child_id,
        title=datos.title,
        date_=datos.date,
        time_=datos.time,
        notes=datos.notes,
        calendar_event_id=calendar_event_id,
    )
    return AppointmentCreatedResponse(ok=True, calendar_event_id=calendar_event_id)

@app.get("/appointments/{child_id}", response_model=AppointmentsResponse)
async def listar_turnos(
    child_id: int,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )
    
    turnos = await crud.get_appointments_by_child(db, child_id)
    return AppointmentsResponse(appointments=[AppointmentOut.model_validate(t) for t in turnos])