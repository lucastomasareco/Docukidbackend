"""
google_oauth.py
----------------
Implementa el flujo de la Guía Técnica, sección 2 ("Flujo OAuth de Google
resumido") y sección 4 (contrato de /auth/google/url y /auth/google/callback).

Resumen del flujo:
  1. El frontend pide GET /auth/google/url (CON su token de Supabase).
  2. Le devolvemos una URL de Google. El frontend la abre en el navegador.
  3. El usuario autoriza el acceso a Drive/Calendar/Gmail.
  4. Google redirige a GET /auth/google/callback -- pero OJO: esta llamada la
     hace el navegador del usuario siguiendo la redirección de Google, NO
     nuestro frontend con su token. Por eso este endpoint NO tiene
     Depends(get_current_user): no llega ninguna cabecera Authorization acá.
  5. Como igual necesitamos saber A QUÉ USUARIO pertenece este refresh_token,
     usamos el parámetro "state": es un valor que nosotros mismos generamos
     en el paso 1 (con el id del usuario adentro, firmado) y que Google nos
     devuelve intacto en el callback. Así "reconectamos" el callback con el
     usuario que empezó el proceso, sin necesitar guardar nada en memoria
     entre medio (importante porque Render puede reiniciar el proceso).

Checklist de la Guía Técnica que este archivo respeta:
  - La URL de autorización incluye access_type=offline Y prompt=consent
    (si falta alguno de los dos, en la segunda vez que el usuario autorice
    Google puede no mandar refresh_token, y el flujo se rompe en silencio).
"""
import os
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# Google devuelve el scope "email" también como
# "https://www.googleapis.com/auth/userinfo.email" (la misma cosa en dos
# formatos distintos). oauthlib compara scope pedido vs. devuelto de forma
# estricta y tira "Scope has changed", que sin esta variable caía en el
# except Exception silencioso del callback. Con RELAX_TOKEN_SCOPE=1 le
# decimos que no sea estricto con esa comparación.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build as google_build
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .auth import get_current_user
from .config import settings
from .database import get_db
from .models import User

router = APIRouter(prefix="/auth/google", tags=["google-oauth"])

GOOGLE_CLIENT_ID = settings.google_client_id
GOOGLE_CLIENT_SECRET = settings.google_client_secret
GOOGLE_REDIRECT_URI = settings.google_redirect_uri

# --- Nota sobre STATE_SECRET ---
# Firmamos el "state" para poder confiar en él cuando Google nos lo devuelva
# en el callback (si no lo firmáramos, cualquiera podría mandar un "state"
# con el user_id que quisiera y pisarle el refresh_token a otro usuario).
# Es una clave INDEPENDIENTE a propósito del mecanismo de firma de JWT de
# Supabase (JWKS): si una se compromete, la otra sigue siendo segura.
# Generala con:
#     python -c "import secrets; print(secrets.token_urlsafe(48))"
# y pegala en tu .env como STATE_SECRET=...
STATE_SECRET = settings.state_secret

# --- Nota sobre FRONTEND_URL ---
# A dónde redirigimos al usuario después del callback, para que la app móvil
# se entere de si quedó todo ok. Con Expo Router, esto normalmente es el
# "deep link" de tu app (algo como "docukids://"). Cuando tengas el scheme
# final en app.json, actualizá también este default.
FRONTEND_URL = settings.frontend_url

# --- Nota sobre ENV ---
# Usado únicamente para decidir si registramos endpoints de desarrollo
# (ver /url-dev al final del archivo). En producción debería valer
# "production" o cualquier cosa distinta de "development".
ENV = settings.env

# Permisos que pedimos: subir archivos propios a Drive, crear eventos de
# Calendar, y enviar correos con Gmail. Pedimos solo lo mínimo necesario
# (no acceso total a todo el Drive del usuario, por ejemplo).
#
# "openid" y "email" NO tocan datos del usuario; son meta-scopes para que
# Google nos diga en userinfo.get() qué email tiene la cuenta que acaba de
# conectar. Lo necesitamos para guardar google_email en la fila del usuario
# y no tener que llamar a people.getProfile() en cada email que enviamos.
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
]


def _build_flow() -> Flow:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI):
        raise HTTPException(
            status_code=500,
            detail="Faltan GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET o GOOGLE_REDIRECT_URI en el .env",
        )

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        # PKCE off: como cada request construye un Flow nuevo (somos
        # stateless para bancarnos un reinicio de Render), el code_verifier
        # que la librería genera en /url-dev se perdería y en el callback
        # Google devuelve "(invalid_grant) Missing code verifier".
        # Nuestro client es de tipo "web" con client_secret, así que PKCE
        # no es obligatorio acá.
        autogenerate_code_verifier=False,
    )


@router.get("/url")
async def get_google_auth_url(usuario: User = Depends(get_current_user)):
    """
    Requiere el token de Supabase (usuario logueado). Devuelve { url } para
    que el frontend la abra en el navegador del celular.
    """
    if not STATE_SECRET:
        raise HTTPException(500, "Falta STATE_SECRET en el .env")

    flow = _build_flow()

    state_payload = {
        "user_id": str(usuario.id),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    state = jwt.encode(state_payload, STATE_SECRET, algorithm="HS256")

    auth_url, _ = flow.authorization_url(
        access_type="offline",   # <- imprescindible para recibir refresh_token
        prompt="consent",         # <- imprescindible para recibirlo TODAS las veces
        state=state,
        include_granted_scopes="true",
    )
    return {"url": auth_url}


@router.get("/status")
async def get_google_status(usuario: User = Depends(get_current_user)):
    """
    Requiere el token de Supabase (usuario logueado). Le dice al frontend si
    esta cuenta YA conectó Google (existe google_refresh_token guardado) o
    no. Nunca devuelve el token en sí, solo un booleano — el frontend lo usa
    para decidir si mostrar el botón "Conectar con Google" en la pantalla de
    inicio (Docs) o si ya no hace falta mostrarlo ahí (en Ajustes se deja
    siempre, por si el usuario necesita reconectar).
    """
    return {"conectado": bool(usuario.google_refresh_token)}


@router.get("/callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """
    A este endpoint lo llama Google (siguiendo la redirección), no nuestro
    frontend directamente. Por eso NO tiene Depends(get_current_user).
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        # El usuario canceló el permiso, o algo falló del lado de Google.
        # quote() por higiene: los valores que devuelve Google son strings
        # controlados, pero así evitamos caracteres raros en el deep link.
        return RedirectResponse(
            f"{FRONTEND_URL}?google=error&detail={quote(error)}"
        )

    if not code or not state:
        raise HTTPException(400, "Faltan 'code' o 'state' en la respuesta de Google")

    if not STATE_SECRET:
        raise HTTPException(500, "Falta STATE_SECRET en el .env")

    try:
        payload = jwt.decode(state, STATE_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        # Lo más probable: pasaron más de 10 minutos, o alguien mandó un
        # state trucho. En ambos casos, mejor frenar acá que guardar cualquier cosa.
        return RedirectResponse(f"{FRONTEND_URL}?google=error&detail=state_invalido")

    # Casteo explícito a UUID: asyncpg es estricto y si el WHERE recibe un
    # str puede tirar DataError al comparar contra la columna UUID.
    user_id = UUID(payload["user_id"])

    flow = _build_flow()
    try:
        # Envuelto en threadpool para no bloquear el event loop mientras
        # Google procesa el intercambio del código por el refresh_token.
        await run_in_threadpool(flow.fetch_token, code=code)
    except Exception as error:
        # Logueamos el error real: sin esto, cualquier fallo del intercambio
        # (scope cambiado, code ya consumido, credenciales mal, etc.) queda
        # tapado por el RedirectResponse y en los logs solo se ve el 307.
        print(f"ERROR en fetch_token: {error}")
        return RedirectResponse(
            f"{FRONTEND_URL}?google=error&detail=intercambio_de_codigo_fallo"
        )

    refresh_token = flow.credentials.refresh_token

    if not refresh_token:
        # Pasa típicamente si el usuario ya había autorizado antes SIN que
        # hayamos mandado prompt=consent esa vez (ver checklist de la Guía
        # Técnica, sección 2). Con access_type=offline+prompt=consent puestos
        # arriba, esto no debería pasar en la práctica.
        return RedirectResponse(f"{FRONTEND_URL}?google=error&detail=sin_refresh_token")

    # Con el access_token que acabamos de conseguir, le preguntamos a Google
    # el email de la cuenta que el usuario acaba de conectar. Lo guardamos
    # en users.google_email para que el notifier no tenga que hacer esta
    # llamada a la People API en cada vencimiento que quiera avisar.
    google_email = None
    try:
        oauth2_service = google_build("oauth2", "v2", credentials=flow.credentials)
        user_info = await run_in_threadpool(
            lambda: oauth2_service.userinfo().get().execute()
        )
        google_email = user_info.get("email")
    except Exception as error:
        # Si falla (ej. Google devuelve 500 en userinfo), no rompemos todo
        # el flujo OAuth por eso: guardamos igual el refresh_token (que es
        # el dato imprescindible) y el notifier lo arregla luego con
        # getProfile() como fallback. Logueamos el error para no quedar
        # a ciegas si algún día deja de funcionar userinfo().
        print(f"ERROR en userinfo: {error}")
        google_email = None

    # Guardamos ambos campos en una sola UPDATE. Solo pisamos google_email
    # si userinfo() devolvió algo no-vacío; de lo contrario dejamos el valor
    # que ya tuviera (útil si este usuario re-conecta Google y el endpoint
    # userinfo() fallara por alguna razón transitoria -- no perdemos el
    # email ya guardado).
    values = {"google_refresh_token": refresh_token}
    if google_email:
        values["google_email"] = google_email
    await db.execute(
        update(User).where(User.id == user_id).values(**values)
    )
    await db.commit()

    return RedirectResponse(f"{FRONTEND_URL}?google=ok")


# ============================================================
# Endpoint SOLO PARA DESARROLLO
# ============================================================
# Igual que /url pero sin requerir autenticación, para poder probar el flujo
# OAuth desde el navegador mientras todavía no tenemos login en la app.
#
# IMPORTANTE: la ruta SOLO se registra si ENV=development. En producción
# (ENV no seteado, o seteado a "production"/"staging"/etc.) esta ruta
# simplemente no existe y FastAPI responde 404. Esto elimina el riesgo de
# que quede expuesta por un olvido antes de un deploy.
#
# Debés pasar un UUID real de tu base de datos (cópialo desde el dashboard
# de Supabase → Authentication → Users).
if ENV == "development":

    @router.get("/url-dev")
    async def get_google_auth_url_dev(user_id: UUID):
        """DEV ONLY: igual que /url pero sin requerir autenticación."""
        if not STATE_SECRET:
            raise HTTPException(500, "Falta STATE_SECRET en el .env")

        flow = _build_flow()

        state_payload = {
            "user_id": str(user_id),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        }
        state = jwt.encode(state_payload, STATE_SECRET, algorithm="HS256")

        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            state=state,
            include_granted_scopes="true",
        )
        return {"url": auth_url}