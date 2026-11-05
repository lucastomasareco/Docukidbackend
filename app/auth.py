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
from .models import Family, User

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


# ----------------------------------------------------------------------
# Familias (v2.5): ¿de QUIÉN son los datos de este pedido?
# ----------------------------------------------------------------------
# Los hijos, documentos, carpetas y turnos están a nombre del TITULAR
# (quien conectó Google). Si quien hace el pedido es el miembro de una
# familia, los endpoints tienen que consultar con el titular, no con él.
#
# get_current_user NO cambia: sigue devolviendo a la persona que hizo el
# pedido (sirve para /family, /account, etc.). Los endpoints de datos usan
# get_data_owner en su lugar:
#
#     @app.get("/children")
#     async def listar_hijos(titular: User = Depends(get_data_owner)):
#         ...
#
# Qué NO hace: no comprueba permisos de titular vs. miembro (eso va en
# cada endpoint de /family). Solo resuelve de quién son los datos.


async def resolver_titular(db: AsyncSession, usuario: User) -> User:
    """
    Devuelve al titular si `usuario` es miembro de una familia; si no, al
    propio `usuario`. Separada de la dependencia para poder probarla sola.

    Ante cualquier dato raro (familia inexistente, titular borrado) devuelve
    al propio usuario: nunca a un tercero ni un error. Así, lo peor que pasa
    es que la persona vea sus propios datos (vacíos), no los de otra familia.
    """
    if usuario.family_id is None:
        return usuario

    familia = await db.get(Family, usuario.family_id)
    if familia is None or familia.owner_id == usuario.id:
        # Sin familia real, o es el propio titular: los datos son suyos.
        return usuario

    titular = await db.get(User, familia.owner_id)
    return titular if titular is not None else usuario


async def get_data_owner(
    usuario: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependencia de FastAPI: el usuario dueño de los datos de este pedido."""
    return await resolver_titular(db, usuario)
