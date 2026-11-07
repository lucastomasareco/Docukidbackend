"""
main.py
Este es el archivo que arrancas para levantar el servidor. Registra los
endpoints de la Fase 1 del plan de construcción (Guía Técnica, sección 7):
CRUD básico, SIN integraciones externas todavía (nada de Google Drive,
Gemini, Google Calendar ni correos: eso es la Fase 2).
Endpoints que vas a encontrar acá (ver contrato completo en Guía Técnica,
sección 4):
GET    /                       -> chequeo simple de que el server está vivo (devuelve la versión)
GET    /auth/google/url        -> genera la URL de autorización de Google (Fase 2, paso 1)
GET    /auth/google/callback   -> recibe la respuesta de Google y guarda el refresh_token (Fase 2, paso 1)
GET    /children               -> lista los hijos del usuario logueado
POST   /children               -> crea un hijo nuevo
PUT    /children/{child_id}    -> edita un hijo (name y/o birth_date)
DELETE /children/{child_id}    -> borra un hijo entero (documentos en Drive, turnos en Calendar y metadatos)
POST   /upload                 -> "sube" un documento (todavía simulado)
GET    /documents/{child_id}   -> lista documentos de un hijo, con status calculado
PATCH  /documents/{doc_id}     -> renombra, confirma la fecha y/o cambia de carpeta un documento
DELETE /documents/{doc_id}     -> borra un documento
GET    /folders                -> lista las carpetas (crea las 3 de base la primera vez)
POST   /folders                -> crea una carpeta (también en Drive)
PATCH  /folders/{id}           -> renombra una carpeta (también en Drive)
DELETE /folders/{id}           -> borra una carpeta (sus archivos pasan a la raíz "Docukids")
POST   /drive/migrate          -> migra de a 10 los documentos viejos a la carpeta de su hijo en Drive (v2.6)
POST   /appointments           -> crea un turno médico (evento real en Google Calendar)
PATCH  /appointments/{id}      -> edita un turno (y su evento en Google Calendar)
DELETE /appointments/{id}      -> borra un turno (y su evento en Google Calendar)
GET    /family                 -> la familia de quien pregunta (o null)
POST   /family                 -> crea una familia (quien llama queda como titular)
POST   /family/invite          -> el titular genera un código de invitación (24 h, un uso)
POST   /family/join            -> unirse a una familia con un código
POST   /family/leave           -> el miembro sale de la familia
DELETE /family/members/{id}    -> el titular quita al miembro
DELETE /family                 -> el titular disuelve la familia
DELETE /account                -> elimina la cuenta: datos de la app + usuario de Supabase Auth. ?delete_drive=true manda 'Docukids' a la papelera de Drive (por defecto false: Drive se conserva). Calendar NO se toca
GET    /appointments/{child_id} -> lista los turnos de un hijo
POST   /scheduler/check         -> lo llama cron-job.org 1 vez al día; manda los avisos pendientes
/upload ahora hace, en este orden: valida el hijo y el tipo de archivo,
sube el archivo a Drive (drive_upload.py), y le pasa el archivo a Gemini
para detectar la fecha de vencimiento (ocr_reader.py).
Con este archivo se completa la Fase 2 completa de la Guía Técnica.
Lo que sigue (Fase 3) es configurar cron-job.org, desplegar en Render, y
probar todo de punta a punta -- ya no es código nuevo del backend.
"""
import logging
import re
import unicodedata
from datetime import date, timezone
from typing import Literal, Optional
from uuid import UUID
import requests
from googleapiclient.errors import HttpError
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from . import crud
from .account import borrar_usuario_de_auth, motivo_de_error, revocar_permiso_de_google
from .auth import get_current_user, get_data_owner
from .config import settings
from .calendar_sync import actualizar_evento, crear_evento, borrar_evento
from .database import get_db
from .drive_recover import escanear_carpetas_recuperables, restaurar_carpeta_de_papelera
from .drive_trash import (
    NoRestaurable,
    listar_documentos_en_papelera,
    leer_documento_de_papelera,
    mandar_documento_a_papelera,
    propiedades_de_documento,
    restaurar_documento_en_drive,
)
from .drive_upload import (
    EXTENSION_POR_MIME,
    PX_MINIATURA,
    RESULTADO_NO_EXISTE,
    SinMiniatura,
    abrir_drive,
    armar_nombre_archivo,
    borrar_carpeta_de_hijo_si_vacia,
    borrar_carpeta_si_vacia,
    borrar_subcarpeta_de_hijo,
    delete_file_from_drive,
    enviar_docukids_a_papelera,
    migrar_archivo,
    mover_archivo_a_carpeta_de_hijo,
    nombre_carpeta_hijo,
    obtener_miniatura_de_drive,
    renombrar_archivo_en_drive,
    renombrar_carpeta_en_drive,
    upload_file_to_child_folder,
)
from .google_oauth import router as google_oauth_router
from .models import User
from .ocr_reader import leer_fecha_de_vencimiento
from .scheduler import router as scheduler_router
from .schemas import (
    AppointmentCreate,
    AppointmentCreatedResponse,
    AppointmentOut,
    AppointmentUpdate,
    AppointmentsResponse,
    ChildCreate,
    ChildOut,
    ChildResponse,
    ChildrenResponse,
    ChildUpdate,
    DeleteResponse,
    DocumentOut,
    DocumentsResponse,
    DocumentUpdate,
    FamilyJoin,
    FamilyMemberOut,
    FamilyOut,
    FamilyResponse,
    FolderCreate,
    FolderOut,
    FolderResponse,
    FoldersResponse,
    FolderUpdate,
    InviteResponse,
    MigrateResponse,
    RecoverCandidate,
    RecoverMatch,
    RecoverPreviewResponse,
    RecoverRequest,
    RecoverResponse,
    RestoreDocumentResponse,
    TrashedDocumentOut,
    TrashedDocumentsResponse,
    UploadResponse,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Docukids API", version="2.6.0")

# La app móvil no corre en un navegador con las mismas reglas que una web,
# pero dejamos CORS abierto para no tener sorpresas si en algún momento se
# prueba la API desde una herramienta web (Swagger UI incluido) o desde un
# emulador. No expone nada que ya no esté protegido por el token JWT.
# CORS_ORIGINS="*" (por defecto) o una lista separada por comas. La autenticación va por
# cabecera Authorization (no cookies), así que no hace falta allow_credentials.
_origenes_cors = [o.strip().rstrip("/") for o in settings.cors_origins.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origenes_cors,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(google_oauth_router)
app.include_router(scheduler_router)

@app.get("/")
async def raiz():
    """Endpoint simple para confirmar que el servidor está corriendo."""
    return {"mensaje": "Docukids API funcionando", "version": app.version}

# ---------- /children ----------
# Familias (v2.5): en estos endpoints `usuario` es el DUEÑO DE LOS DATOS
# (get_data_owner): el titular si quien llama es miembro de una familia, o la
# propia persona si no tiene familia. Con eso los dos ven los mismos hijos, y
# Drive y Calendar se usan con las credenciales del titular.
@app.get("/children", response_model=ChildrenResponse)
async def listar_hijos(
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    hijos = await crud.get_children_by_user(db, usuario.id)
    return ChildrenResponse(children=[ChildOut.model_validate(h) for h in hijos])

@app.post("/children", response_model=ChildResponse)
async def crear_hijo(
    datos: ChildCreate,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    hijo = await crud.create_child(
        db, usuario.id, datos.name, datos.birth_date, avatar_url=datos.avatar_url
    )
    return ChildResponse(child=ChildOut.model_validate(hijo))

@app.put("/children/{child_id}", response_model=ChildResponse)
async def editar_hijo(
    child_id: int,
    datos: ChildUpdate,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )
    cambios = datos.model_dump(exclude_unset=True)
    # Drive por hijo (v2.6): si cambia el nombre, se renombra su carpeta en Drive
    # ANTES de guardar (si Drive falla, no se guarda nada). Los archivos ya
    # subidos conservan el nombre viejo: el ID del hijo en el nombre los sigue
    # identificando. Si el hijo todavía no tiene carpeta, se creará con el
    # nombre nuevo al primer uso.
    if "name" in cambios and hijo.drive_folder_id and cambios["name"].strip() != hijo.name:
        await run_in_threadpool(
            renombrar_carpeta_en_drive,
            usuario,
            hijo.drive_folder_id,
            nombre_carpeta_hijo(hijo.id, cambios["name"].strip()),
        )
    hijo = await crud.update_child(db, hijo, cambios)
    return ChildResponse(child=ChildOut.model_validate(hijo))

@app.delete("/children/{child_id}", response_model=DeleteResponse)
async def borrar_hijo(
    child_id: int,
    usuario: User = Depends(get_data_owner),
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

    # Drive por hijo (v2.6): con sus archivos ya borrados, se limpia su carpeta
    # (y sus subcarpetas) SOLO si quedaron vacías. Si la persona puso algo a
    # mano, eso no se toca. Un error de Google (que no sea 404) corta acá y no
    # se borra nada de la base.
    sub_ids = await crud.get_child_folder_drive_ids(db, child_id)
    if hijo.drive_folder_id or sub_ids:
        await run_in_threadpool(
            borrar_carpeta_de_hijo_si_vacia, usuario, hijo.drive_folder_id, sub_ids
        )

    await crud.delete_child(db, hijo)
    return DeleteResponse(message="Deleted")

# ---------- /upload ----------
# Familias (v2.5): en /upload y en /documents `usuario` es el DUEÑO DE LOS DATOS
# (get_data_owner), igual que en /children. Los archivos se suben, se leen y se
# borran en el Drive del titular, tanto si lo pide él como su familiar.
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

async def _limpiar_archivo_de_drive(usuario: User, drive_file_id: str) -> None:
    """
    Borra de Drive un archivo recién subido cuyo registro no se pudo guardar.
    Nunca lanza excepción: si el borrado también falla, solo lo registra
    (con el ID del archivo, que no es un dato personal) para poder
    limpiarlo a mano.
    """
    try:
        await run_in_threadpool(delete_file_from_drive, usuario, drive_file_id)
    except Exception:
        logger.error("Archivo huérfano en Drive (no se pudo borrar): %s", drive_file_id)


@app.post("/upload", response_model=UploadResponse)
async def subir_documento(
    file: UploadFile = File(...),
    child_id: int = Form(...),
    name: str = Form(...),
    type: Optional[str] = Form(None),
    folder_id: Optional[int] = Form(None),
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    # Primero confirmamos que el hijo exista Y sea del usuario que llama.
    hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese child_id no existe o no pertenece a tu usuario",
        )
    
    # Si viene folder_id, tiene que ser una carpeta de esta persona (ajena = 404).
    carpeta = None
    if folder_id is not None:
        carpeta = await _carpeta_propia_o_404(db, folder_id, usuario.id)

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

    # --- Subida real a Drive, a la carpeta del hijo (v2.6, Guía §13) ---
    # Drive: Docukids / {IDhijo}_{Nombre} / {Carpeta} / archivo. Si la persona
    # todavía no conectó Google, drive_upload levanta un 400/401 claro.
    # Guardamos ya los datos del hijo: tras un rollback, SQLAlchemy "olvida" los
    # valores de los objetos cargados y no se pueden volver a leer.
    hijo_id = hijo.id
    hijo_nombre = hijo.name
    ext = EXTENSION_POR_MIME.get(mime_type)
    # El ID del documento no existe todavía: nombre provisional, que se
    # renombra con el ID después del INSERT.
    nombre_provisional = armar_nombre_archivo(hijo_id, hijo_nombre, name, None, ext)
    sub_guardada = (
        await crud.get_child_folder_drive_id(db, hijo_id, carpeta.id) if carpeta is not None else None
    )
    drive_file_id, drive_link, hijo_drive_vigente, sub_vigente = await run_in_threadpool(
        upload_file_to_child_folder,
        user=usuario,
        file_bytes=contenido,
        filename=nombre_provisional,
        mime_type=mime_type,
        child_id=hijo_id,
        child_name=hijo_nombre,
        child_drive_folder_id=hijo.drive_folder_id,
        folder_name=carpeta.name if carpeta is not None else None,
        child_folder_drive_id=sub_guardada,
    )
    try:
        await crud.set_child_drive_folder_id(db, hijo, hijo_drive_vigente)
        if carpeta is not None and sub_vigente:
            await crud.set_child_folder_drive_id(db, hijo_id, carpeta.id, sub_vigente)
    except Exception:
        # No es grave: la próxima vez se vuelve a buscar o crear la carpeta.
        logger.warning("No se pudo guardar el id de Drive de la carpeta del hijo %s.", hijo_id)
        await db.rollback()
        try:
            await db.refresh(usuario)  # el rollback también "olvida" al usuario
        except Exception:
            pass

    # --- Fase 2, paso 3: OCR con Gemini para detectar la fecha de vencimiento ---
    # Nunca lanza excepción (ver ocr_reader.py): si falla, expiry_date queda
    # en None y el documento se guarda igual, con status "sin_fecha".
    fecha_vencimiento = await run_in_threadpool(
        leer_fecha_de_vencimiento, contenido, mime_type
    )
    
    # Si guardar en la base falla, el archivo ya está en Drive pero ningún
    # documento lo apunta (queda "huérfano"). Lo borramos de Drive para no
    # dejar basura en la cuenta de la persona y devolvemos un error claro.
    try:
        documento = await crud.create_document(
            db,
            child_id=child_id,
            name=name,
            type_=type,
            drive_file_id=drive_file_id,
            drive_link=drive_link,
            expiry_date=fecha_vencimiento,
            folder_id=folder_id,
            mime_type=mime_type,
        )
    except Exception:
        logger.exception("Falló el INSERT del documento; se intenta limpiar el archivo de Drive.")
        try:
            await db.rollback()
        except Exception:
            logger.warning("No se pudo hacer rollback tras el fallo del INSERT.")
        await _limpiar_archivo_de_drive(usuario, drive_file_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo guardar el documento. No se guardó nada; probá de nuevo.",
        )

    # Ahora que existe el ID del documento, el archivo en Drive lo lleva en el
    # nombre. Si falla NO se deshace nada: el archivo ya está en la carpeta del
    # hijo y su nombre lleva el ID del hijo.
    # La respuesta se arma ANTES del renombrado: así nada de lo que pase después
    # puede impedir devolverla (el documento ya está guardado).
    respuesta = UploadResponse(
        status=crud.compute_status(documento.expiry_date, documento.no_expiry),
        expiry_date=documento.expiry_date,
        no_expiry=documento.no_expiry,
        drive_link=documento.drive_link,
        doc_id=documento.id,
        folder_id=documento.folder_id,
        mime_type=documento.mime_type,
    )
    doc_id_nuevo = documento.id

    # Si el renombrado sale bien, el documento ya está en su lugar y con su nombre
    # definitivo: se marca como migrado. Si falla, queda pendiente y POST /drive/migrate
    # lo arregla (Guía §13, regla 7).
    try:
        nombre_final = armar_nombre_archivo(hijo_id, hijo_nombre, name, doc_id_nuevo, ext)
        renombrado = await run_in_threadpool(renombrar_archivo_en_drive, usuario, drive_file_id, nombre_final)
        if renombrado:
            await crud.mark_document_migrated(db, documento)
    except Exception:
        logger.warning("No se pudo poner el ID del documento %s en el nombre del archivo de Drive.", doc_id_nuevo)

    return respuesta

# ---------- /documents ----------
@app.get("/documents/{child_id}", response_model=DocumentsResponse)
async def listar_documentos(
    child_id: int,
    usuario: User = Depends(get_data_owner),
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
            no_expiry=documento.no_expiry,
            status=crud.compute_status(documento.expiry_date, documento.no_expiry),
            drive_link=documento.drive_link,
            folder_id=documento.folder_id,
            mime_type=documento.mime_type,
        )
        for documento in documentos
    ]
    return DocumentsResponse(documents=salida)

@app.patch("/documents/{doc_id}", response_model=DocumentOut)
async def actualizar_documento(
    doc_id: int,
    datos: DocumentUpdate,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """Cambia el nombre y/o confirma o corrige el vencimiento (ver Guía: PATCH /documents/{doc_id})."""
    documento = await crud.get_document_owned_by_user(db, doc_id, usuario.id)
    if documento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese doc_id no existe o no pertenece a tu usuario",
        )

    # Cambio de carpeta: primero Drive y después la base (si Drive falla, no se guarda nada).
    cambiar_carpeta = "folder_id" in datos.model_fields_set
    if cambiar_carpeta and datos.folder_id != documento.folder_id:
        carpeta = None
        if datos.folder_id is not None:
            carpeta = await _carpeta_propia_o_404(db, datos.folder_id, usuario.id)
        if documento.drive_file_id:
            # Se mueve DENTRO del mismo hijo: nunca cambia de hijo (Guía §13, regla 2).
            hijo = await crud.get_child_owned_by_user(db, documento.child_id, usuario.id)
            if hijo is None:  # no debería pasar (el documento ya se validó como propio)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Ese doc_id no existe o no pertenece a tu usuario",
                )
            sub_guardada = (
                await crud.get_child_folder_drive_id(db, hijo.id, carpeta.id) if carpeta else None
            )
            hijo_vigente, sub_vigente = await run_in_threadpool(
                mover_archivo_a_carpeta_de_hijo,
                usuario,
                documento.drive_file_id,
                hijo.id,
                hijo.name,
                hijo.drive_folder_id,
                carpeta.name if carpeta else None,
                sub_guardada,
            )
            await crud.set_child_drive_folder_id(db, hijo, hijo_vigente)
            if carpeta is not None and sub_vigente:
                await crud.set_child_folder_drive_id(db, hijo.id, carpeta.id, sub_vigente)

    documento = await crud.update_document(
        db,
        documento,
        name=datos.name,
        expiry_date=datos.expiry_date,
        no_expiry=bool(datos.no_expiry),
        cambiar_carpeta=cambiar_carpeta,
        folder_id=datos.folder_id,
    )
    return DocumentOut(
        id=documento.id,
        name=documento.name,
        expiry_date=documento.expiry_date,
        no_expiry=documento.no_expiry,
        status=crud.compute_status(documento.expiry_date, documento.no_expiry),
        drive_link=documento.drive_link,
        folder_id=documento.folder_id,
        mime_type=documento.mime_type,
    )


@app.get("/documents/{doc_id}/thumbnail")
async def miniatura_de_documento(
    doc_id: int,
    size: Literal["small", "large"] = "small",
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Devuelve la IMAGEN (no JSON) del documento: miniatura chica para la lista
    (size=small, 240 px) o versión grande para el visor (size=large, 1600 px).
    La baja el backend de Drive con las credenciales de la persona; la app nunca
    ve enlaces ni tokens de Google. Ver Guía v2.4.
    """
    documento = await crud.get_document_owned_by_user(db, doc_id, usuario.id)
    if documento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese doc_id no existe o no pertenece a tu usuario",
        )
    sin_miniatura = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Este documento no tiene miniatura (todavía).",
    )
    if not documento.drive_file_id:
        raise sin_miniatura

    mime_archivo: Optional[str] = None
    try:
        contenido, tipo, mime_archivo = await run_in_threadpool(
            obtener_miniatura_de_drive, usuario, documento.drive_file_id, PX_MINIATURA[size]
        )
    except SinMiniatura as sin:
        mime_archivo = sin.mime_type
        error: Optional[HTTPException] = sin_miniatura
    except (HttpError, requests.RequestException):
        logger.warning("No se pudo obtener la miniatura del documento %s.", documento.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo obtener la imagen de Drive. Probá de nuevo.",
        )
    else:
        error = None

    # Documentos anteriores a v2.4: aprovechamos y guardamos su tipo.
    if documento.mime_type is None and mime_archivo:
        try:
            await crud.set_document_mime_type(db, documento, mime_archivo)
        except Exception:
            logger.warning("No se pudo guardar el tipo del documento %s.", documento.id)
            await db.rollback()

    if error is not None:
        raise error
    return Response(
        content=contenido,
        media_type=tipo,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.delete("/documents/{doc_id}", response_model=DeleteResponse)
async def borrar_documento(
    doc_id: int,
    usuario: User = Depends(get_data_owner),
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
    # v3.0: el archivo ya NO se elimina para siempre: va a la papelera de Drive (30 días) llevando
    # consigo los datos que solo viven en la base (tipo, vencimiento, carpeta), para poder
    # restaurarlo desde GET/POST /trash/documents.
    en_papelera = False
    if documento.drive_file_id:
        en_papelera = await run_in_threadpool(
            mandar_documento_a_papelera,
            usuario,
            documento.drive_file_id,
            propiedades_de_documento(documento),
        )
    await crud.delete_document(db, documento)
    return DeleteResponse(message="DeletedToTrash" if en_papelera else "Deleted")


# ---------- /trash/documents (v3.0) ----------
# Rutas aparte de /documents/{child_id} para que "trash" nunca se confunda con un child_id.
@app.get("/trash/documents", response_model=TrashedDocumentsResponse)
async def listar_documentos_borrados(
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """Documentos borrados que todavía están en la papelera de Drive (máx. 30 días)."""
    if not usuario.google_refresh_token:
        return TrashedDocumentsResponse(documents=[])
    try:
        archivos = await run_in_threadpool(listar_documentos_en_papelera, usuario)
    except HTTPException:
        raise
    except Exception as error:
        logger.error("No se pudo leer la papelera de Drive: %s", motivo_de_error(error))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo leer la papelera de Drive. Probá de nuevo en unos segundos.",
        )

    hijos = {h.id: h.name for h in await crud.get_children_by_user(db, usuario.id)}
    salida = []
    for a in archivos:
        props = a["props"]
        try:
            hijo_id = int(props.get("dk_child", ""))
        except ValueError:
            continue
        if hijo_id not in hijos:
            continue  # el hijo ya no existe (o no es de esta persona): no hay dónde restaurarlo
        try:
            carpeta_id = int(props["dk_folder"]) if props.get("dk_folder") else None
        except ValueError:
            carpeta_id = None
        try:
            vence = date.fromisoformat(props["dk_exp"]) if props.get("dk_exp") else None
        except ValueError:
            vence = None
        salida.append(
            TrashedDocumentOut(
                drive_file_id=a["drive_file_id"],
                name=props.get("dk_name") or a["name"],
                type=props.get("dk_type") or None,
                child_id=hijo_id,
                child_name=hijos[hijo_id],
                folder_id=carpeta_id,
                expiry_date=vence,
                no_expiry=props.get("dk_noexp") == "1",
                mime_type=a["mime_type"],
                days_left=a["days_left"],
            )
        )
    salida.sort(key=lambda d: d.days_left)  # los que están por vencer en la papelera, primero
    return TrashedDocumentsResponse(documents=salida)


@app.post("/trash/documents/{drive_file_id}/restore", response_model=RestoreDocumentResponse)
async def restaurar_documento_borrado(
    drive_file_id: str,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """Saca un documento de la papelera de Drive y vuelve a crearlo con sus datos."""
    no_esta = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Ese documento ya no está en la papelera de Drive (pasaron más de 30 días o se vació).",
    )
    try:
        archivo = await run_in_threadpool(leer_documento_de_papelera, usuario, drive_file_id)
    except NoRestaurable:
        raise no_esta

    props = archivo.get("appProperties") or {}
    try:
        hijo_id_pedido = int(props.get("dk_child", ""))
    except ValueError:
        raise no_esta
    hijo = await crud.get_child_owned_by_user(db, hijo_id_pedido, usuario.id)
    if hijo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El hijo de este documento ya no existe, así que no se puede restaurar.",
        )
    # Si la carpeta (Salud, Escuela...) ya no existe, el documento vuelve suelto, como pasa en la base.
    carpeta = None
    try:
        if props.get("dk_folder"):
            carpeta = await crud.get_folder_owned_by_user(db, int(props["dk_folder"]), usuario.id)
    except ValueError:
        carpeta = None
    try:
        vence = date.fromisoformat(props["dk_exp"]) if props.get("dk_exp") else None
    except ValueError:
        vence = None
    sin_vencimiento = props.get("dk_noexp") == "1"

    # Guardamos ya lo que hace falta: tras un rollback SQLAlchemy "olvida" los objetos cargados.
    hijo_id, hijo_nombre, hijo_drive = hijo.id, hijo.name, hijo.drive_folder_id
    carpeta_id = carpeta.id if carpeta is not None else None
    carpeta_nombre = carpeta.name if carpeta is not None else None
    nombre = props.get("dk_name") or archivo.get("name") or "documento"
    tipo = props.get("dk_type") or None
    sub_guardada = await crud.get_child_folder_drive_id(db, hijo_id, carpeta_id) if carpeta_id is not None else None

    try:
        resultado = await run_in_threadpool(
            restaurar_documento_en_drive,
            usuario, archivo, hijo_id, hijo_nombre, hijo_drive, carpeta_nombre, sub_guardada,
        )
    except NoRestaurable:
        raise no_esta

    try:
        await crud.set_child_drive_folder_id(db, hijo, resultado["hijo_vigente"])
        if carpeta_id is not None and resultado["sub_vigente"]:
            await crud.set_child_folder_drive_id(db, hijo_id, carpeta_id, resultado["sub_vigente"])
    except Exception:
        logger.warning("No se pudo guardar el id de Drive de la carpeta del hijo %s.", hijo_id)
        await db.rollback()
        try:
            await db.refresh(usuario)
        except Exception:
            pass

    try:
        documento = await crud.create_document(
            db,
            child_id=hijo_id,
            name=nombre,
            type_=tipo,
            drive_file_id=drive_file_id,
            drive_link=resultado["drive_link"],
            expiry_date=vence,
            folder_id=carpeta_id,
            mime_type=resultado["mime_type"],
        )
        if sin_vencimiento:
            documento = await crud.update_document_expiry(db, documento, None, True)
    except Exception:
        logger.exception("Falló el INSERT del documento restaurado; se vuelve a mandar su archivo a la papelera.")
        try:
            await db.rollback()
        except Exception:
            pass
        try:
            await run_in_threadpool(mandar_documento_a_papelera, usuario, drive_file_id, props)
        except Exception:
            logger.error("Archivo restaurado sin registro en la base (no se pudo devolver a la papelera): %s", drive_file_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo restaurar el documento. Sigue en la papelera de Drive; probá de nuevo.",
        )

    respuesta = RestoreDocumentResponse(doc_id=documento.id, child_id=hijo_id, name=documento.name)
    doc_id_nuevo = documento.id

    # El nombre del archivo lleva el ID del documento, que ahora es uno nuevo. Mejor esfuerzo:
    # si falla no pasa nada grave (POST /drive/migrate lo corrige) y la respuesta ya está armada.
    try:
        ext = EXTENSION_POR_MIME.get(resultado["mime_type"] or "")
        nombre_final = armar_nombre_archivo(hijo_id, hijo_nombre, nombre, doc_id_nuevo, ext)
        renombrado = await run_in_threadpool(renombrar_archivo_en_drive, usuario, drive_file_id, nombre_final)
        if renombrado:
            await crud.mark_document_migrated(db, documento)
    except Exception:
        logger.warning("No se pudo renombrar el archivo restaurado del documento %s.", doc_id_nuevo)

    return respuesta

# ---------- /folders ----------
# Familias (v2.5): igual que en /children y /documents, `usuario` es el DUEÑO DE
# LOS DATOS (get_data_owner). Las carpetas son las del titular, y también sus
# subcarpetas en Drive: las dos personas de la familia ven y editan las mismas.
async def _carpeta_propia_o_404(db: AsyncSession, folder_id: int, user_id):
    carpeta = await crud.get_folder_owned_by_user(db, folder_id, user_id)
    if carpeta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese folder_id no existe o no pertenece a tu usuario",
        )
    return carpeta


def _nombre_repetido() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Ya tenés una carpeta con ese nombre.",
    )


@app.get("/folders", response_model=FoldersResponse)
async def listar_carpetas(
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """Lista las carpetas. La primera vez crea Documentos, Salud y Escuela (solo en la base)."""
    carpetas = await crud.get_folders_ensuring_base(db, usuario.id)
    return FoldersResponse(folders=[FolderOut.model_validate(c) for c in carpetas])


@app.post("/folders", response_model=FolderResponse)
async def crear_carpeta(
    datos: FolderCreate,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Crea una carpeta SOLO en la base. Drive por hijo (v2.6): la subcarpeta de
    cada hijo se crea al primer uso (al subir o mover un documento a ella).
    """
    if await crud.folder_name_exists(db, usuario.id, datos.name):
        raise _nombre_repetido()
    try:
        carpeta = await crud.create_folder(db, usuario.id, datos.name)
    except IntegrityError:
        await db.rollback()
        raise _nombre_repetido()
    return FolderResponse(folder=FolderOut.model_validate(carpeta))


@app.patch("/folders/{folder_id}", response_model=FolderResponse)
async def renombrar_carpeta(
    folder_id: int,
    datos: FolderUpdate,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """Renombra la carpeta (también las de base) y la subcarpeta de cada hijo. Primero Drive y después la base."""
    carpeta = await _carpeta_propia_o_404(db, folder_id, usuario.id)
    if datos.name == carpeta.name:
        return FolderResponse(folder=FolderOut.model_validate(carpeta))
    if await crud.folder_name_exists(db, usuario.id, datos.name, exclude_id=carpeta.id):
        raise _nombre_repetido()

    # Drive por hijo (v2.6): se renombra la subcarpeta de CADA hijo que ya exista.
    # Si Google falla en alguna, no se guarda nada en la base (se puede reintentar).
    for fila in await crud.get_child_folders_of_folder(db, carpeta.id):
        await run_in_threadpool(renombrar_carpeta_en_drive, usuario, fila.drive_folder_id, datos.name)
    try:
        carpeta = await crud.rename_folder(db, carpeta, datos.name)
    except IntegrityError:
        await db.rollback()
        raise _nombre_repetido()
    return FolderResponse(folder=FolderOut.model_validate(carpeta))


@app.delete("/folders/{folder_id}", response_model=DeleteResponse)
async def borrar_carpeta(
    folder_id: int,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Borra una carpeta que no sea de base. NUNCA borra documentos. Drive por hijo
    (v2.6): para cada hijo, sus archivos de esa carpeta pasan a la carpeta DE ESE
    HIJO y se borra su subcarpeta si quedó vacía; después se borra la fila (los
    documentos quedan con folder_id = NULL). Si Google falla por algo distinto
    de un 404, no se borra nada de la base.
    """
    carpeta = await _carpeta_propia_o_404(db, folder_id, usuario.id)
    if carpeta.is_base:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Las carpetas Documentos, Salud y Escuela no se pueden borrar (sí renombrar).",
        )

    documentos = await crud.get_documents_in_folder(db, carpeta.id)
    archivos_por_hijo = {}
    for d in documentos:
        if d.drive_file_id:
            archivos_por_hijo.setdefault(d.child_id, []).append(d.drive_file_id)
    subcarpetas = {f.child_id: f.drive_folder_id for f in await crud.get_child_folders_of_folder(db, carpeta.id)}

    for child_id in set(archivos_por_hijo) | set(subcarpetas):
        hijo = await crud.get_child_owned_by_user(db, child_id, usuario.id)
        if hijo is None:  # no debería pasar; si pasara, no hay a quién mover nada
            continue
        hijo_vigente = await run_in_threadpool(
            borrar_subcarpeta_de_hijo,
            usuario,
            hijo.id,
            hijo.name,
            hijo.drive_folder_id,
            subcarpetas.get(child_id),
            archivos_por_hijo.get(child_id, []),
        )
        if hijo_vigente:
            await crud.set_child_drive_folder_id(db, hijo, hijo_vigente)

    # Carpeta de la estructura anterior a la v2.6 (ya sin uso): se borra solo si quedó vacía.
    if carpeta.drive_folder_id:
        await run_in_threadpool(borrar_carpeta_si_vacia, usuario, carpeta.drive_folder_id)

    await crud.delete_folder(db, carpeta)
    return DeleteResponse(message="Deleted")

# ---------- /drive/migrate (Drive por hijo, v2.6, Guía §13 regla 7) ----------
# Cuántos documentos viejos se procesan por llamada. Un lote chico evita que la
# llamada pase del tiempo máximo de Render (cada documento es 1 o 2 pedidos a Drive).
_LOTE_MIGRACION = 10


async def _limpiar_carpetas_viejas(db: AsyncSession, usuario: User) -> None:
    """
    Cuando ya no queda nada por migrar: borra de Drive las carpetas de la estructura
    anterior ("Docukids / Salud", guardadas en folders.drive_folder_id) SOLO si están
    vacías, y las olvida en la base. Si la persona dejó algo adentro, se conservan y
    se vuelven a revisar en la próxima llamada.
    """
    for carpeta in await crud.get_folders_by_user(db, usuario.id):
        if not carpeta.drive_folder_id:
            continue
        ya_no_existe = await run_in_threadpool(borrar_carpeta_si_vacia, usuario, carpeta.drive_folder_id)
        if ya_no_existe:
            await crud.set_folder_drive_id(db, carpeta, None)


@app.post("/drive/migrate", response_model=MigrateResponse)
async def migrar_drive(
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Mueve y renombra los documentos subidos antes de la v2.6 a la carpeta de su hijo,
    de a lotes de 10. Se vuelve a llamar hasta que `remaining` sea 0. Es idempotente:
    repetirlo no rompe nada. El documento NUNCA se borra: si su archivo ya no está en
    Drive se cuenta en `skipped` y se da por revisado. Si Google falla (que no sea un
    404) corta con error; lo ya migrado queda marcado y la próxima llamada sigue.
    """
    filas = await crud.get_documents_to_migrate(db, usuario.id, _LOTE_MIGRACION)
    migrated = 0
    skipped = 0

    if filas:
        carpetas = {c.id: c for c in await crud.get_folders_by_user(db, usuario.id)}
        drive = await run_in_threadpool(abrir_drive, usuario)
        for documento, hijo in filas:
            carpeta = carpetas.get(documento.folder_id) if documento.folder_id is not None else None
            sub_guardada = (
                await crud.get_child_folder_drive_id(db, hijo.id, carpeta.id) if carpeta is not None else None
            )
            resultado, hijo_vigente, sub_vigente = await run_in_threadpool(
                migrar_archivo,
                drive,
                documento.drive_file_id,
                hijo.id,
                hijo.name,
                hijo.drive_folder_id,
                carpeta.name if carpeta is not None else None,
                sub_guardada,
                documento.name,
                documento.id,
            )
            if resultado == RESULTADO_NO_EXISTE:
                skipped += 1
            else:
                migrated += 1
                # Se guardan primero los ids de las carpetas y recién después se marca el
                # documento: así, si algo falla, no se pierde la carpeta ya creada.
                await crud.set_child_drive_folder_id(db, hijo, hijo_vigente)
                if carpeta is not None and sub_vigente:
                    await crud.set_child_folder_drive_id(db, hijo.id, carpeta.id, sub_vigente)
            await crud.mark_document_migrated(db, documento)

    remaining = await crud.count_documents_to_migrate(db, usuario.id)
    if remaining == 0:
        await _limpiar_carpetas_viejas(db, usuario)
    return MigrateResponse(migrated=migrated, skipped=skipped, remaining=remaining)


# ---------- Recuperar datos tras re-registro (v2.8) ----------
SUFIJO_RECUPERADO = " - recuperado"


def _clave_nombre(nombre: str) -> str:
    """Para comparar nombres: sin tildes, sin mayúsculas y sin espacios de más ('Lucía' = 'lucia')."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", nombre or "") if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_tildes.casefold().split())


def _hijo_con_mismo_nombre(hijos: list, nombre: str):
    """
    El hijo que ya existe con ese nombre. Si hay varios, se prefiere uno que todavía no
    tenga carpeta propia en Drive (al que se le puede vincular la carpeta vieja).
    """
    clave = _clave_nombre(nombre)
    iguales = [h for h in hijos if _clave_nombre(h.name) == clave]
    if not iguales:
        return None
    return next((h for h in iguales if not h.drive_folder_id), iguales[0])


def _contar_documentos(entrada: dict) -> int:
    return len(entrada["files"]) + sum(len(sub["files"]) for sub in entrada["subfolders"])


@app.get("/drive/recover/preview", response_model=RecoverPreviewResponse)
async def recuperar_vista_previa(
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Solo lectura: busca en el Drive conectado carpetas '{ID}_{Nombre}' que quedaron de una
    cuenta anterior y que ningún hijo actual tiene vinculadas. La app lo usa para decidir
    si ofrece "Recuperar datos". No crea ni modifica nada.
    """
    vinculadas = await crud.get_linked_child_drive_folder_ids(db, usuario.id)
    encontradas = await run_in_threadpool(escanear_carpetas_recuperables, usuario, vinculadas)
    hijos = list(await crud.get_children_by_user(db, usuario.id))
    candidatas = []
    for e in encontradas:
        # Si dos carpetas viejas se llaman igual, la segunda ya no puede vincularse al mismo hijo.
        existente = _hijo_con_mismo_nombre(hijos, e["name"])
        match = None
        if existente is not None:
            puede = not existente.drive_folder_id and not any(
                c.match is not None and c.match.child_id == existente.id and c.match.can_link
                for c in candidatas
            )
            match = RecoverMatch(child_id=existente.id, name=existente.name, can_link=puede)
        candidatas.append(RecoverCandidate(
            drive_folder_id=e["drive_folder_id"],
            name=e["name"],
            old_child_id=e["old_child_id"],
            documents=_contar_documentos(e),
            folders=[sub["name"] for sub in e["subfolders"]],
            match=match,
        ))
    return RecoverPreviewResponse(candidates=candidatas)


async def _buscar_o_crear_carpeta_de_app(db: AsyncSession, usuario: User, nombre: str, existentes: list):
    """Carpeta de la app con ese nombre (sin mirar mayúsculas); si no existe, la crea. Nunca duplica."""
    for c in existentes:
        if c.name.lower() == nombre.lower():
            return c
    try:
        carpeta = await crud.create_folder(db, usuario.id, nombre)
    except IntegrityError:  # otro pedido la creó justo ahora
        await db.rollback()
        existentes[:] = list(await crud.get_folders_by_user(db, usuario.id))
        for c in existentes:
            if c.name.lower() == nombre.lower():
                return c
        raise
    existentes.append(carpeta)
    return carpeta


async def _recuperar_un_hijo(
    db: AsyncSession, usuario: User, entrada: dict, carpetas_app: list,
    existente=None, nombre: Optional[str] = None,
):
    """
    Reconecta UNA carpeta vieja de Drive. Con `existente` (un hijo que la persona ya tiene
    y que todavía no tiene carpeta en Drive) la vincula a ese hijo; sin él, crea un hijo.
    La carpeta del hijo se vincula AL FINAL: si algo falla a mitad, se deshace lo hecho
    (el hijo creado se borra; en uno existente solo se borran los documentos de este intento)
    y se puede reintentar sin duplicados, porque mientras no esté vinculada la carpeta sigue
    apareciendo como recuperable.
    """
    if existente is not None:
        hijo = existente
    else:
        hijo = await crud.create_child(db, usuario.id, nombre or entrada["name"], None)
    hijo_id = hijo.id  # después de un rollback el objeto queda vencido: se guarda el id aparte
    creados = []
    try:
        pendientes = [(None, entrada["files"])] + [(s, s["files"]) for s in entrada["subfolders"]]
        for sub, archivos in pendientes:
            carpeta = None
            nombre_sub = (sub["name"] or "").strip() if sub else ""
            if sub is not None and nombre_sub:
                carpeta = await _buscar_o_crear_carpeta_de_app(db, usuario, nombre_sub, carpetas_app)
                await crud.set_child_folder_drive_id(db, hijo_id, carpeta.id, sub["drive_folder_id"])
            for f in archivos:
                creados.append(await crud.create_document(
                    db, hijo_id, f["doc_name"], None,
                    drive_file_id=f["drive_file_id"], drive_link=f["drive_link"],
                    folder_id=carpeta.id if carpeta is not None else None,
                    mime_type=f["mime_type"],
                ))
        await crud.set_child_drive_folder_id(db, hijo, entrada["drive_folder_id"])
    except Exception:
        await db.rollback()
        if existente is None:
            a_borrar = await crud.get_child_owned_by_user(db, hijo_id, usuario.id)
            if a_borrar is not None:
                await crud.delete_child(db, a_borrar)
        else:
            for doc in creados:
                try:
                    await crud.delete_document(db, doc)
                except Exception:
                    pass
        raise
    return hijo, len(creados)


@app.post("/drive/recover", response_model=RecoverResponse)
async def recuperar_datos(
    datos: RecoverRequest,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Vincula carpetas viejas de Drive a hijos NUEVOS, en vez de crear carpetas paralelas.
    Por cada carpeta elegida: crea el hijo, apunta children.drive_folder_id a la carpeta
    vieja, reutiliza las carpetas de la app por nombre (child_folders) y reconecta cada
    archivo como documento (sin fecha de vencimiento: esa vivía solo en la base).
    Los documentos quedan con drive_migrated=false: la app llama a POST /drive/migrate
    hasta remaining=0 para renombrarlos con los IDs nuevos. No mueve ni borra nada en Drive.
    Los archivos se leen de Drive en el servidor; de la app solo se aceptan ids de carpeta.
    """
    vinculadas = await crud.get_linked_child_drive_folder_ids(db, usuario.id)
    encontradas = await run_in_threadpool(escanear_carpetas_recuperables, usuario, vinculadas)
    por_id = {e["drive_folder_id"]: e for e in encontradas}

    carpetas_app = list(await crud.get_folders_ensuring_base(db, usuario.id))
    hijos_actuales = list(await crud.get_children_by_user(db, usuario.id))
    recuperados = []
    documentos = 0
    saltados = 0
    vinculados = 0
    for drive_id in dict.fromkeys(datos.folders):  # sin repetidos, en orden
        entrada = por_id.get(drive_id)
        if entrada is None:
            saltados += 1
            continue
        # Mismo nombre que un hijo que ya existe: si ese hijo no tiene carpeta propia en Drive
        # se le vincula la carpeta vieja (sin hijos repetidos); si ya tiene archivos propios no
        # se mezcla nada: la carpeta vieja se recupera como un perfil aparte, con otro nombre.
        # La carpeta viene de la PAPELERA de Drive (cuenta eliminada con "mandar a la papelera"):
        # se saca de ahí ANTES de tocar la base. Si falla, no se creó nada y se puede reintentar.
        if entrada.get("en_papelera"):
            try:
                await run_in_threadpool(restaurar_carpeta_de_papelera, usuario, entrada)
            except HTTPException:
                raise
            except Exception as error:
                logger.error("No se pudo sacar la carpeta de la papelera de Drive (%s).", type(error).__name__)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="No se pudo sacar tu carpeta de la papelera de Drive. No se recuperó nada: probá de nuevo.",
                )
        existente = _hijo_con_mismo_nombre(hijos_actuales, entrada["name"])
        vincular = existente if (existente is not None and not existente.drive_folder_id) else None
        nombre = entrada["name"] + SUFIJO_RECUPERADO if (existente is not None and vincular is None) else None
        try:
            hijo, cantidad = await _recuperar_un_hijo(
                db, usuario, entrada, carpetas_app, existente=vincular, nombre=nombre
            )
        except Exception as error:
            logger.error("No se pudo recuperar una carpeta de hijo (%s).", type(error).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No se pudo recuperar una de las carpetas. Lo ya recuperado quedó guardado: probá de nuevo.",
            )
        if vincular is None:
            hijos_actuales.append(hijo)
        else:
            vinculados += 1
        recuperados.append(ChildOut.model_validate(hijo))
        documentos += cantidad
        # Que la carpeta vieja siga la convención '{IDnuevo}_{Nombre}' (mejor esfuerzo; el
        # vínculo es por id, así que si falla igual funciona).
        try:
            await run_in_threadpool(
                renombrar_carpeta_en_drive, usuario, drive_id, nombre_carpeta_hijo(hijo.id, hijo.name)
            )
        except Exception as error:
            logger.warning("No se pudo renombrar la carpeta recuperada (%s).", type(error).__name__)

    remaining = await crud.count_documents_to_migrate(db, usuario.id)
    return RecoverResponse(
        recovered=recuperados, documents=documentos, skipped=saltados, remaining=remaining, linked=vinculados
    )


# ---------- /appointments ----------
# Familias (v2.5): igual que en los grupos anteriores, `usuario` es el DUEÑO DE
# LOS DATOS (get_data_owner). Los turnos son los del titular y los eventos se
# crean, editan y borran en el Google Calendar del titular.
@app.post("/appointments", response_model=AppointmentCreatedResponse)
async def crear_turno(
    datos: AppointmentCreate,
    usuario: User = Depends(get_data_owner),
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
    usuario: User = Depends(get_data_owner),
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


@app.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
async def actualizar_turno(
    appointment_id: int,
    datos: AppointmentUpdate,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Edita un turno (ver Guía §4: PATCH /appointments/{appointment_id}).
    Se mandan solo los campos que cambian. Se actualiza primero el evento en
    Google Calendar y recién después la base; si Google falla, no se guarda nada.
    """
    turno = await crud.get_appointment_owned_by_user(db, appointment_id, usuario.id)
    if turno is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese appointment_id no existe o no pertenece a tu usuario",
        )

    cambios = datos.model_dump(exclude_unset=True)
    titulo = cambios["title"].strip() if "title" in cambios else turno.title
    fecha = cambios["date"] if "date" in cambios else turno.date
    hora = cambios["time"] if "time" in cambios else turno.time
    notas = cambios["notes"] if "notes" in cambios else turno.notes
    if notas is not None:
        notas = notas.strip() or None

    calendar_event_id = await run_in_threadpool(
        actualizar_evento,
        user=usuario,
        calendar_event_id=turno.calendar_event_id,
        title=titulo,
        fecha=fecha,
        hora=hora,
        notes=notas,
    )
    turno = await crud.update_appointment(
        db,
        turno,
        title=titulo,
        date_=fecha,
        time_=hora,
        notes=notas,
        calendar_event_id=calendar_event_id,
    )
    return AppointmentOut.model_validate(turno)


@app.delete("/appointments/{appointment_id}", response_model=DeleteResponse)
async def borrar_turno(
    appointment_id: int,
    usuario: User = Depends(get_data_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Borra un turno (ver Guía §4: DELETE /appointments/{appointment_id}).
    Primero el evento de Google Calendar (si ya no existe, se ignora) y después
    la fila. Si Google falla por otro motivo, NO se borra nada de la base.
    """
    turno = await crud.get_appointment_owned_by_user(db, appointment_id, usuario.id)
    if turno is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese appointment_id no existe o no pertenece a tu usuario",
        )

    if turno.calendar_event_id:
        await run_in_threadpool(borrar_evento, usuario, turno.calendar_event_id)

    await crud.delete_appointment(db, turno)
    return DeleteResponse(message="Deleted")

# ---------- /family (v2.5) ----------
# Estos endpoints usan get_current_user (la PERSONA que llama), no get_data_owner:
# hablan de quién es quién en la familia, no de los datos de los hijos.

def _como_utc(momento):
    """La base guarda UTC sin zona; al enviarlo se marca como UTC (+00:00) para que la app no lo lea como hora local."""
    return momento.replace(tzinfo=timezone.utc)


async def _familia_para_responder(db: AsyncSession, user_id, familia) -> FamilyOut:
    """Arma la familia tal como la ve `user_id`. El código solo lo ve el titular y solo si sigue vigente."""
    miembros = await crud.get_family_members(db, familia.id)
    # El titular siempre va primero en la lista.
    miembros = sorted(miembros, key=lambda u: u.id != familia.owner_id)
    es_titular = familia.owner_id == user_id

    salida = FamilyOut(
        id=familia.id,
        role="owner" if es_titular else "member",
        members=[
            FamilyMemberOut(
                id=u.id,
                email=u.email,
                role="owner" if u.id == familia.owner_id else "member",
            )
            for u in miembros
        ],
    )
    if es_titular and crud.invitacion_vigente(familia):
        salida.invite_code = familia.invite_code
        salida.invite_expires_at = _como_utc(familia.invite_expires_at)
    return salida


@app.get("/family", response_model=FamilyResponse)
async def ver_familia(
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """La familia de quien pregunta, o { family: null } si no tiene."""
    if usuario.family_id is None:
        return FamilyResponse(family=None)
    familia = await crud.get_family(db, usuario.family_id)
    if familia is None:
        return FamilyResponse(family=None)
    return FamilyResponse(family=await _familia_para_responder(db, usuario.id, familia))


@app.post("/family", response_model=FamilyResponse)
async def crear_familia(
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Crea una familia nueva; quien llama queda como titular. Solo si todavía no tiene una."""
    ya_en_familia = HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Ya formás parte de una familia.",
    )
    if usuario.family_id is not None:
        raise ya_en_familia
    user_id = usuario.id
    familia = await crud.create_family(db, usuario)
    if familia is None:
        raise ya_en_familia
    return FamilyResponse(family=await _familia_para_responder(db, user_id, familia))


@app.post("/family/invite", response_model=InviteResponse)
async def invitar_a_la_familia(
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Solo el titular. Genera un código de 6 caracteres que vence en 24 h y sirve
    una sola vez. Si ya había uno, este lo reemplaza (el anterior deja de servir).
    """
    sin_familia = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Todavía no tenés una familia. Creala primero.",
    )
    if usuario.family_id is None:
        raise sin_familia
    familia = await crud.get_family(db, usuario.family_id)
    if familia is None:
        raise sin_familia
    if familia.owner_id != usuario.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el titular de la familia puede generar códigos de invitación.",
        )

    family_id = familia.id
    try:
        codigo, vence = await crud.create_invite(db, family_id)
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo generar el código. Probá de nuevo en un momento.",
        )
    return InviteResponse(invite_code=codigo, invite_expires_at=_como_utc(vence))


async def _familia_como_titular(db: AsyncSession, usuario: User):
    """La familia de `usuario`, pero solo si es su titular. Si no: 404 (sin familia) o 403 (es miembro)."""
    if usuario.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Todavía no tenés una familia.",
        )
    familia = await crud.get_family(db, usuario.family_id)
    if familia is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Todavía no tenés una familia.",
        )
    if familia.owner_id != usuario.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el titular de la familia puede hacer esto.",
        )
    return familia


@app.post("/family/join", response_model=FamilyResponse)
async def unirse_a_la_familia(
    datos: FamilyJoin,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Unirse a una familia con un código. Reglas (Guía §12):
      - Código inexistente, vencido o ya usado: 404 (no se distingue).
      - Familia con 2 personas: 409.
      - Si la persona ya tiene hijos propios: 409 (para no perderlos sin querer).
      - Si tenía Google conectado por su cuenta, se desconecta: desde ahora usa el del titular.
    El código no se escribe en ningún log.
    """
    if usuario.family_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya formás parte de una familia.",
        )

    codigo_invalido = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Ese código no existe, venció o ya se usó.",
    )
    codigo = datos.code.strip().upper()
    familia = await crud.get_family_by_active_code(db, codigo)
    if familia is None:
        raise codigo_invalido

    miembros = await crud.get_family_members(db, familia.id)
    if len(miembros) >= crud.MAX_FAMILIA:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esa familia ya está completa (máximo 2 personas).",
        )
    if await crud.user_has_own_data(db, usuario.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya tenés hijos cargados en tu cuenta. Borralos antes de unirte a una familia, así no se pierden sin querer.",
        )

    user_id = usuario.id
    family_id = familia.id
    token_propio = usuario.google_refresh_token  # se guarda antes de que join_family lo borre
    if not await crud.join_family(db, usuario, family_id, codigo):
        raise codigo_invalido
    if token_propio:
        await revocar_permiso_de_google(token_propio)  # mejor esfuerzo, nunca falla

    await db.refresh(familia)
    return FamilyResponse(family=await _familia_para_responder(db, user_id, familia))


@app.post("/family/leave", response_model=DeleteResponse)
async def salir_de_la_familia(
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """El miembro sale de la familia y queda con una cuenta vacía. El titular no puede salir: tiene que disolverla."""
    if usuario.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No formás parte de ninguna familia.",
        )
    familia = await crud.get_family(db, usuario.family_id)
    if familia is not None and familia.owner_id == usuario.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El titular no puede salir de su familia. Si querés terminarla, disolvela.",
        )
    await crud.remove_from_family(db, usuario)
    return DeleteResponse(message="Left")


@app.delete("/family/members/{user_id}", response_model=DeleteResponse)
async def quitar_miembro(
    user_id: UUID,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Solo el titular. Saca a un miembro de la familia; ese miembro queda con una cuenta vacía."""
    familia = await _familia_como_titular(db, usuario)
    if user_id == usuario.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El titular no puede quitarse a sí mismo. Si querés terminar la familia, disolvela.",
        )
    miembro = await crud.get_user_by_id(db, user_id)
    if miembro is None or miembro.family_id != familia.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Esa persona no es parte de tu familia.",
        )
    await crud.remove_from_family(db, miembro)
    return DeleteResponse(message="Deleted")


@app.delete("/family", response_model=DeleteResponse)
async def disolver_familia(
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Solo el titular. Disuelve la familia: el miembro queda con su cuenta vacía y los datos siguen con el titular."""
    familia = await _familia_como_titular(db, usuario)
    await crud.dissolve_family(db, familia.id)
    return DeleteResponse(message="Deleted")

# ---------- /account ----------
@app.delete("/account", response_model=DeleteResponse)
async def eliminar_cuenta(
    delete_drive: bool = False,
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Elimina la cuenta de quien llama.
      - Se borran sus hijos, documentos y turnos de la base, y su usuario de Supabase Auth.
      - Si es titular de una familia, la familia se disuelve; si es miembro, solo sale de ella.
      - Archivos de Google Drive (v2.7, Guía §14): por defecto NO se tocan. Con
        ?delete_drive=true la carpeta 'Docukids' va a la PAPELERA de Drive (no se
        borra para siempre). Solo vale para el titular o quien no tiene familia: si
        elimina un MIEMBRO se ignora, porque sus subidas están en el Drive del titular.
      - Los eventos de Google Calendar nunca se borran.
      - Permiso de Google: se revoca (mejor esfuerzo) SOLO si delete_drive=true. Si
        conserva Drive no se revoca, para poder recuperar sus carpetas al volver a
        registrarse (ver comentario más abajo). Sin usuario, el scheduler ya no le
        manda correos.
    Orden: primero la base, después Supabase Auth. Si Auth falla (502), la
    persona puede reintentar: no queda ningún dato suyo en la base.
    """
    if not settings.supabase_secret_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falta SUPABASE_SECRET_KEY en el servidor; no se eliminó nada.",
        )

    user_id = usuario.id
    refresh_token = usuario.google_refresh_token
    sin_mover_en_drive = 0  # elementos de Drive que la app no pudo mandar a la papelera

    # Drive (v2.7, Guía §14): se hace PRIMERO, antes de tocar la base y de revocar el
    # permiso de Google (después ya no habría token ni se sabría de quién es la carpeta).
    # Si Drive falla, no se borra nada y la persona puede reintentar.
    if delete_drive and refresh_token:
        es_miembro = False
        if usuario.family_id is not None:
            familia_drive = await crud.get_family(db, usuario.family_id)
            es_miembro = familia_drive is not None and familia_drive.owner_id != usuario.id
        if not es_miembro:
            try:
                sin_mover_en_drive = await run_in_threadpool(enviar_docukids_a_papelera, usuario)
            except HTTPException as error_http:
                # 401 = Google ya no deja renovar el acceso (permiso revocado, token vencido, app en
                # modo "Testing"...). Si se dejara pasar como 401, la app diría "Google desconectado
                # -> Ir a Ajustes" y la persona no podría eliminar su cuenta. Se traduce a un aviso
                # que explica la causa y ofrece la salida. No se borra nada.
                if error_http.status_code == 401:
                    logger.error("No se pudo mandar Docukids a la papelera: Google no renovó el acceso (401).")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=(
                            "Google ya no nos deja acceder a tu Drive (el permiso venció o fue quitado), "
                            "así que no se pudo mover tu carpeta a la papelera. No se eliminó nada. "
                            "Elegí «Conservarlos en mi Drive» para eliminar la cuenta igual: tus archivos "
                            "quedan en tu Drive y podés borrarlos a mano."
                        ),
                    )
                raise
            except Exception as error:
                motivo = motivo_de_error(error)
                logger.error("No se pudo mandar Docukids a la papelera de Drive (%s).", motivo)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        f"No se pudieron mover tus archivos de Drive a la papelera ({motivo}). "
                        "No se eliminó nada. Probá de nuevo, o elegí «Conservarlos en mi Drive» para eliminar la cuenta igual."
                    ),
                )

    # Familias (v2.5, regla 6): si quien elimina es el TITULAR, se disuelve la
    # familia y el miembro queda con una cuenta vacía. Si es el MIEMBRO, solo
    # se va: sus datos propios (vacíos) se borran y los del titular no se tocan.
    # El miembro no tiene token de Google propio, así que nunca se revoca el del titular.
    if usuario.family_id is not None:
        familia = await crud.get_family(db, usuario.family_id)
        if familia is not None and familia.owner_id == usuario.id:
            await crud.dissolve_family(db, familia.id)

    await crud.delete_user(db, usuario)

    # v2.8 (recuperar datos): con "conservar Drive" NO se revoca el permiso de Google. La app
    # usa el alcance drive.file, y Google quita el acceso a los archivos que la app creó en
    # cuanto la persona des-autoriza la app: si se revocara acá, al volver a registrarse la
    # app ya no podría ver la carpeta 'Docukids' vieja y no habría nada que recuperar. El
    # token guardado se borra igual junto con el usuario, así que la app no puede actuar
    # sobre ese Drive; la persona puede quitar el permiso cuando quiera desde su cuenta de
    # Google. Con delete_drive=true la carpeta ya va a la papelera, así que sí se revoca.
    # v2.9: tampoco con delete_drive=true se revoca. Si se revocara, la app perdería el acceso a
    # la carpeta que acaba de mandar a la papelera y no podría recuperarla al volver a registrarse.
    # El token guardado se borró junto con el usuario: la app no puede actuar sobre ese Drive.

    await borrar_usuario_de_auth(user_id)
    if sin_mover_en_drive:
        logger.warning("Cuenta eliminada; %s elemento(s) de Drive quedaron sin mover a la papelera (sin permiso).", sin_mover_en_drive)
        return DeleteResponse(message="DeletedPartial")
    return DeleteResponse(message="Deleted")
