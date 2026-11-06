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
# Drive por hijo (v2.6, Guía §13)
# ---------------------------------------------------------------------------
# Estructura nueva:
#     Docukids / {IDhijo}_{Nombre} / {Carpeta} / {IDhijo}_{Nombre}_{Doc}_{IDdoc}.{ext}
#     ej.:  Docukids / 12_Ana / Salud / 12_Ana_Cartilla_57.pdf
# El ID del hijo (único en toda la base) evita que dos hijos con el mismo
# nombre compartan carpeta o tengan archivos indistinguibles.
# Las carpetas se guardan y se buscan SIEMPRE por ID de Drive, nunca por nombre.
# Las funciones de esta sección son nuevas: las usan los pasos 3 a 6 del plan.

# Extensión del archivo según el tipo real detectado (magic numbers en /upload).
EXTENSION_POR_MIME = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
}

_MAX_NOMBRE_DRIVE = 255  # largo máximo de un nombre en Drive


def sanear_nombre(s: str) -> str:
    """Quita caracteres peligrosos o de ruta. Misma regla que _safe_filename de main.py."""
    s = re.sub(r"[^\w\s\-.]", "_", s or "", flags=re.UNICODE)
    return s.strip()[:200] or "documento"


def nombre_carpeta_hijo(child_id: int, child_name: str) -> str:
    """Nombre de la carpeta del hijo: '{IDhijo}_{Nombre}'. Ej.: '12_Ana'."""
    return f"{child_id}_{sanear_nombre(child_name)}"


def armar_nombre_archivo(
    child_id: int,
    child_name: str,
    doc_name: str,
    doc_id: Optional[int] = None,
    ext: Optional[str] = None,
) -> str:
    """
    Nombre del archivo en Drive: '{IDhijo}_{Nombre}_{Doc}_{IDdoc}.{ext}'.
    Ej.: '12_Ana_Cartilla_57.pdf'.
    - doc_id=None arma el nombre PROVISIONAL de la subida (sin ID de documento,
      que recién existe después del INSERT); luego se renombra con el ID.
    - ext=None no agrega extensión. Si el nombre del documento ya termina con
      esa extensión (ej. 'DNI.pdf'), no se repite ('.pdf.pdf').
    - El resultado nunca supera los 255 caracteres de Drive: se recorta la
      parte {Doc}, nunca el ID ni la extensión.
    """
    ext = (ext or "").lstrip(".").lower()
    doc = sanear_nombre(doc_name)
    if ext and doc.lower().endswith("." + ext):
        doc = doc[: -(len(ext) + 1)].rstrip() or "documento"

    prefijo = nombre_carpeta_hijo(child_id, child_name)
    sufijo = (f"_{doc_id}" if doc_id is not None else "") + (f".{ext}" if ext else "")
    # +1 por el "_" que separa el prefijo del {Doc}
    espacio_doc = _MAX_NOMBRE_DRIVE - len(prefijo) - 1 - len(sufijo)
    if espacio_doc < 1:  # nombre de hijo absurdamente largo: se recorta el prefijo
        prefijo = prefijo[: max(1, _MAX_NOMBRE_DRIVE - len(sufijo) - 2)]
        espacio_doc = _MAX_NOMBRE_DRIVE - len(prefijo) - 1 - len(sufijo)
    return f"{prefijo}_{doc[: max(1, espacio_doc)]}{sufijo}"


def _crear_carpeta_en(drive_service, nombre: str, padre_id: str) -> str:
    """Crea la carpeta `nombre` dentro de `padre_id` y devuelve su id."""
    metadata = {"name": nombre, "mimeType": _MIME_CARPETA, "parents": [padre_id]}
    carpeta = drive_service.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


def _asegurar_carpeta_hijo(
    drive_service, child_id: int, child_name: str, drive_folder_id: Optional[str]
) -> str:
    """
    Devuelve el id de la carpeta del hijo, que EXISTE: la guardada, o una nueva
    dentro de 'Docukids' si nunca se creó o la borraron a mano / está en la papelera.
    """
    if drive_folder_id and _subcarpeta_vive(drive_service, drive_folder_id):
        return drive_folder_id
    raiz_id = _get_or_create_carpeta(drive_service)
    return _crear_carpeta_en(drive_service, nombre_carpeta_hijo(child_id, child_name), raiz_id)


def _asegurar_subcarpeta_de_hijo(
    drive_service, carpeta_hijo_id: str, nombre: str, drive_folder_id: Optional[str]
) -> str:
    """
    Devuelve el id de la subcarpeta `nombre` DENTRO de la carpeta del hijo, que
    EXISTE: la guardada, o una nueva si falta. Si la guardada vive pero está
    fuera de la carpeta de este hijo no se reutiliza (nunca se mezclan hermanos).
    """
    if drive_folder_id and _subcarpeta_vive_en(drive_service, drive_folder_id, carpeta_hijo_id):
        return drive_folder_id
    return _crear_carpeta_en(drive_service, nombre, carpeta_hijo_id)


def _subcarpeta_vive_en(drive_service, drive_folder_id: str, padre_id: str) -> bool:
    """True si la carpeta existe, no está en la papelera y su padre es `padre_id`."""
    try:
        carpeta = drive_service.files().get(
            fileId=drive_folder_id, fields="id, trashed, parents"
        ).execute()
    except HttpError as error:
        if _status(error) == 404:
            return False
        raise
    return (not carpeta.get("trashed", False)) and padre_id in (carpeta.get("parents") or [])


@traducir_permisos_de_google("Google Drive")
def upload_file_to_child_folder(
    user: User,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    child_id: int,
    child_name: str,
    child_drive_folder_id: Optional[str],
    folder_name: Optional[str] = None,
    child_folder_drive_id: Optional[str] = None,
) -> Tuple[str, str, str, Optional[str]]:
    """
    Sube el archivo a la carpeta del hijo (la crea si falta) y, si el documento
    tiene carpeta de la app (`folder_name`), a la subcarpeta de ESE hijo.
    Sin `folder_name`, el archivo queda directo en la carpeta del hijo.

    Devuelve (drive_file_id, drive_link, id_carpeta_hijo_vigente,
    id_subcarpeta_vigente_o_None). El llamador debe guardar los ids vigentes
    que hayan cambiado (children.drive_folder_id y child_folders).
    """
    drive_service = _drive(user)
    hijo_id = _asegurar_carpeta_hijo(drive_service, child_id, child_name, child_drive_folder_id)
    destino_id = hijo_id
    sub_id = None
    if folder_name is not None:
        sub_id = _asegurar_subcarpeta_de_hijo(drive_service, hijo_id, folder_name, child_folder_drive_id)
        destino_id = sub_id
    archivo_id, link = _subir(drive_service, destino_id, file_bytes, filename, mime_type)
    return archivo_id, link, hijo_id, sub_id


@traducir_permisos_de_google("Google Drive")
def renombrar_archivo_en_drive(user: User, drive_file_id: str, nuevo_nombre: str) -> bool:
    """
    Cambia el nombre de un archivo en Drive (se usa para ponerle el ID del
    documento después del INSERT). Devuelve True si se renombró y False si el
    archivo ya no existe (404). Otros errores se propagan: el llamador decide
    (en /upload no se deshace nada si el renombrado falla).
    """
    drive_service = _drive(user)
    try:
        drive_service.files().update(
            fileId=drive_file_id, body={"name": nuevo_nombre}, fields="id"
        ).execute()
    except HttpError as error:
        if _status(error) == 404:
            logger.info("Archivo %s ya no existe en Drive; no se renombra.", drive_file_id)
            return False
        raise
    return True


@traducir_permisos_de_google("Google Drive")
def mover_archivo_a_carpeta_de_hijo(
    user: User,
    drive_file_id: str,
    child_id: int,
    child_name: str,
    child_drive_folder_id: Optional[str],
    folder_name: Optional[str],
    child_folder_drive_id: Optional[str],
) -> Tuple[str, Optional[str]]:
    """
    Mueve el archivo DENTRO del mismo hijo: a su subcarpeta `folder_name`, o
    directo a la carpeta del hijo si folder_name es None. Nunca cambia de hijo.
    Devuelve (id_carpeta_hijo_vigente, id_subcarpeta_vigente_o_None); el llamador
    guarda los que hayan cambiado. Si el archivo ya no existe en Drive (404),
    no se mueve nada y no es error.
    """
    drive_service = _drive(user)
    hijo_id = _asegurar_carpeta_hijo(drive_service, child_id, child_name, child_drive_folder_id)
    destino_id = hijo_id
    sub_id = None
    if folder_name is not None:
        sub_id = _asegurar_subcarpeta_de_hijo(drive_service, hijo_id, folder_name, child_folder_drive_id)
        destino_id = sub_id
    try:
        _mover(drive_service, drive_file_id, destino_id)
    except HttpError as error:
        if _status(error) != 404:
            raise
        logger.info("Archivo %s ya no existe en Drive; no se mueve.", drive_file_id)
    return hijo_id, sub_id


def _carpeta_vacia(drive_service, carpeta_id: str) -> bool:
    """True si la carpeta no tiene nada adentro (sin contar lo que está en la papelera)."""
    resultado = drive_service.files().list(
        q=f"'{carpeta_id}' in parents and trashed=false",
        spaces="drive",
        fields="files(id)",
        pageSize=1,
    ).execute()
    return not resultado.get("files")


def _borrar_carpeta_si_vacia(drive_service, carpeta_id: str) -> bool:
    """
    Borra la carpeta SOLO si está vacía. Devuelve True si ya no existe (la
    borró o ya no estaba) y False si se conserva porque tiene algo adentro.
    Un 404 se ignora; cualquier otro error se propaga.
    """
    try:
        if not _carpeta_vacia(drive_service, carpeta_id):
            return False
        drive_service.files().delete(fileId=carpeta_id).execute()
    except HttpError as error:
        if _status(error) != 404:
            raise
    return True


@traducir_permisos_de_google("Google Drive")
def borrar_carpeta_de_hijo_si_vacia(
    user: User,
    child_drive_folder_id: Optional[str],
    sub_drive_folder_ids: List[str],
) -> bool:
    """
    Al borrar un hijo (sus archivos ya se borraron): limpia su carpeta de Drive.
    Primero borra las subcarpetas conocidas del hijo (Salud, Escuela, ...) que
    estén vacías y después la carpeta del hijo, también solo si quedó vacía.
    Si la persona puso algo a mano, eso NO se toca: la carpeta (y la subcarpeta
    que lo contiene) se conservan. Devuelve True si la carpeta del hijo ya no
    existe. Un 404 de Google se ignora; otro error se propaga para que el
    llamador no borre nada de la base.
    """
    drive_service = _drive(user)
    for sub_id in sub_drive_folder_ids:
        _borrar_carpeta_si_vacia(drive_service, sub_id)
    if not child_drive_folder_id:
        return False
    return _borrar_carpeta_si_vacia(drive_service, child_drive_folder_id)


@traducir_permisos_de_google("Google Drive")
def borrar_subcarpeta_de_hijo(
    user: User,
    child_id: int,
    child_name: str,
    child_drive_folder_id: Optional[str],
    sub_drive_folder_id: Optional[str],
    drive_file_ids: List[str],
) -> Optional[str]:
    """
    Al borrar una carpeta de la app, para UN hijo: sus archivos pasan a la
    carpeta de ese hijo (nunca a la de un hermano ni a la raíz) y después se
    borra su subcarpeta, SOLO si quedó vacía (si la persona puso algo a mano, se
    conserva). Nunca se borra un archivo. Un 404 (archivo o carpeta que ya no
    existe) se ignora; cualquier otro error se propaga para que el llamador no
    borre nada de la base.
    Devuelve el id vigente de la carpeta del hijo (None si no hizo falta
    tocarla); el llamador lo guarda si cambió.
    """
    drive_service = _drive(user)
    hijo_id = child_drive_folder_id
    if drive_file_ids:
        hijo_id = _asegurar_carpeta_hijo(drive_service, child_id, child_name, child_drive_folder_id)
        for archivo_id in drive_file_ids:
            try:
                _mover(drive_service, archivo_id, hijo_id)
            except HttpError as error:
                if _status(error) == 404:
                    continue
                raise
    if sub_drive_folder_id:
        _borrar_carpeta_si_vacia(drive_service, sub_drive_folder_id)
    return hijo_id


@traducir_permisos_de_google("Google Drive")
def borrar_carpeta_si_vacia(user: User, drive_folder_id: str) -> bool:
    """
    Borra una carpeta de Drive SOLO si está vacía (se usa con las carpetas de la
    estructura anterior a la v2.6). Devuelve True si ya no existe.
    """
    return _borrar_carpeta_si_vacia(_drive(user), drive_folder_id)


# ---------------------------------------------------------------------------
# Migración de documentos viejos (v2.6, paso 6): POST /drive/migrate
# ---------------------------------------------------------------------------
# Los documentos subidos ANTES de la v2.6 están en "Docukids / {Carpeta}" (o sueltos
# en "Docukids") y se llaman "{hijo} - {doc}", sin extensión ni ID. migrar_archivo
# arregla UN archivo: lo deja en "Docukids / {IDhijo}_{Nombre} / {Carpeta}" con el
# nombre "{IDhijo}_{Nombre}_{Doc}_{IDdoc}.{ext}". Los lotes los arma main.py.

RESULTADO_MIGRADO = "migrado"      # se movió y/o se renombró
RESULTADO_YA_ESTABA = "ya_estaba"  # ya estaba en su lugar y con su nombre: no se tocó
RESULTADO_NO_EXISTE = "no_existe"  # el archivo ya no está en Drive (o está en la papelera)


@traducir_permisos_de_google("Google Drive")
def abrir_drive(user: User):
    """
    Abre el cliente de Drive UNA vez, para reutilizarlo en todo un lote de migración
    (armar las credenciales en cada documento sería lento).
    """
    return _drive(user)


@traducir_permisos_de_google("Google Drive")
def migrar_archivo(
    drive_service,
    drive_file_id: str,
    child_id: int,
    child_name: str,
    child_drive_folder_id: Optional[str],
    folder_name: Optional[str],
    child_folder_drive_id: Optional[str],
    doc_name: str,
    doc_id: int,
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Deja UN archivo viejo en la carpeta de su hijo y con su nombre nuevo.

    Hace una consulta a Drive (dónde está, cómo se llama, qué tipo es) y, solo si
    hace falta, UNA actualización que lo mueve y lo renombra a la vez. La extensión
    sale del tipo que informa Drive (sirve también para documentos sin mime_type).

    Devuelve (resultado, id_carpeta_hijo_vigente, id_subcarpeta_vigente_o_None):
      - "migrado":   se movió y/o se renombró.
      - "ya_estaba": ya estaba bien; no se tocó nada.
      - "no_existe": el archivo no está en Drive o está en la papelera. En ese caso
                     NO se crea ninguna carpeta y se devuelven los ids tal como
                     llegaron. El documento nunca se borra: solo se da por revisado.
    El llamador guarda los ids vigentes que hayan cambiado (igual que en /upload).
    Un error de Google que no sea 404 se propaga: se vuelve a llamar y sigue.
    """
    try:
        archivo = drive_service.files().get(
            fileId=drive_file_id, fields="id, name, mimeType, parents, trashed"
        ).execute()
    except HttpError as error:
        if _status(error) == 404:
            logger.info("Archivo %s ya no existe en Drive; se salta.", drive_file_id)
            return RESULTADO_NO_EXISTE, child_drive_folder_id, child_folder_drive_id
        raise
    if archivo.get("trashed", False):
        logger.info("Archivo %s está en la papelera; se salta.", drive_file_id)
        return RESULTADO_NO_EXISTE, child_drive_folder_id, child_folder_drive_id

    hijo_id = _asegurar_carpeta_hijo(drive_service, child_id, child_name, child_drive_folder_id)
    destino_id = hijo_id
    sub_id = None
    if folder_name is not None:
        sub_id = _asegurar_subcarpeta_de_hijo(drive_service, hijo_id, folder_name, child_folder_drive_id)
        destino_id = sub_id

    ext = EXTENSION_POR_MIME.get(archivo.get("mimeType") or "")
    nombre_nuevo = armar_nombre_archivo(child_id, child_name, doc_name, doc_id, ext)

    padres_actuales = archivo.get("parents") or []
    ya_en_su_carpeta = padres_actuales == [destino_id]
    if ya_en_su_carpeta and archivo.get("name") == nombre_nuevo:
        return RESULTADO_YA_ESTABA, hijo_id, sub_id

    cambios = {"fileId": drive_file_id, "body": {"name": nombre_nuevo}, "fields": "id"}
    if not ya_en_su_carpeta:
        cambios["addParents"] = destino_id
        sobran = [p for p in padres_actuales if p != destino_id]
        if sobran:
            cambios["removeParents"] = ",".join(sobran)
    try:
        drive_service.files().update(**cambios).execute()
    except HttpError as error:
        if _status(error) == 404:  # lo borraron justo ahora
            return RESULTADO_NO_EXISTE, child_drive_folder_id, child_folder_drive_id
        raise
    return RESULTADO_MIGRADO, hijo_id, sub_id


# ---------------------------------------------------------------------------
# Eliminar cuenta con Drive (v2.7, Guía §14): DELETE /account?delete_drive=true
# ---------------------------------------------------------------------------

def _buscar_carpetas_raiz(drive_service) -> List[str]:
    """Ids de las carpetas 'Docukids' vivas. A diferencia de _get_or_create_carpeta, NUNCA crea nada."""
    query = (
        f"name='{DRIVE_FOLDER_NAME}' and mimeType='{_MIME_CARPETA}' and trashed=false"
    )
    resultado = drive_service.files().list(
        q=query, spaces="drive", fields="files(id)"
    ).execute()
    return [f["id"] for f in resultado.get("files", [])]


@traducir_permisos_de_google("Google Drive")
def enviar_docukids_a_papelera(user: User) -> int:
    """
    Manda a la PAPELERA de Drive la carpeta 'Docukids' del usuario (con ella van las
    carpetas de cada hijo y sus archivos). No borra nada para siempre: la persona puede
    recuperarlo desde la papelera durante unos 30 días. Si 'Docukids' no existe (la
    borró a mano o nunca subió nada) no es error. Un 404 se ignora; cualquier otro
    error se propaga para que el llamador NO borre nada de la base.
    Devuelve cuántas carpetas mandó a la papelera.
    """
    drive_service = _drive(user)
    enviadas = 0
    for carpeta_id in _buscar_carpetas_raiz(drive_service):
        try:
            drive_service.files().update(
                fileId=carpeta_id, body={"trashed": True}, fields="id"
            ).execute()
            enviadas += 1
        except HttpError as error:
            if _status(error) != 404:
                raise
    return enviadas


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
