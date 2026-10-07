"""
auth.py
-------
Verifica, en cada endpoint protegido, que el token que llega en
"Authorization: Bearer <token>" sea un access token válido emitido por
Supabase Auth.

Desde las nuevas "signing keys" de Supabase, los JWT de usuario se firman
con algoritmos ASIMÉTRICOS (ES256/RS256). Ya NO se verifica con un secreto
compartido (HS256). En su lugar, se descargan las claves públicas desde el
endpoint JWKS del proyecto y se valida la firma contra la clave que
corresponda al "kid" del token.

Uso en un endpoint:

    @app.get("/children")
    async def listar_hijos(usuario: User = Depends(get_current_user)):
        ...
"""

import jwt
from jwt import PyJWKClient, PyJWKClientError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .config import settings
from .database import get_db
from .models import User

# PyJWKClient cachea las claves públicas y las refresca automáticamente
# cuando aparece un "kid" desconocido (rotación de claves de Supabase).
# ATENCIÓN: PyJWKClient es SINCRÓNICO. Nunca llamarlo directo desde un
# endpoint async; siempre vía run_in_threadpool (ver get_current_user).
_jwks_client = PyJWKClient(settings.supabase_jwks_url)

# Exige "Authorization: Bearer ...". Si falta, FastAPI responde 403 solo.
_security_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_security_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials

    try:
        # 1) Busca en el JWKS la clave pública cuyo "kid" coincide con el del
        #    token. Es una llamada bloqueante (descarga el JWKS si no está
        #    cacheado), así que la corremos en un threadpool para no congelar
        #    el event loop.
        signing_key = await run_in_threadpool(
            _jwks_client.get_signing_key_from_jwt, token
        )

        # 2) Verifica firma + expiración + audience.
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except PyJWKClientError as error:
        # No pudimos obtener la clave pública (JWKS inalcanzable, kid no
        # encontrado, etc.). Es un problema NUESTRO o de Supabase, no del
        # token del usuario. Devolvemos 503 y no 401 para no mandar al
        # cliente a un loop de "refrescar el token".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo validar el token en este momento",
        )
    except jwt.PyJWTError as error:
        # Firma inválida, token vencido, audience incorrecto, etc.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido o vencido: {error}",
        )

    user_id = payload.get("sub")  # UUID del usuario en Supabase Auth
    email = payload.get("email")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token no incluye un id de usuario (sub)",
        )

    # Alta perezosa: la primera vez que este usuario válido llama a un endpoint
    # protegido, creamos su fila en "users". No agrega columnas ni tablas.
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(id=user_id, email=email)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return user