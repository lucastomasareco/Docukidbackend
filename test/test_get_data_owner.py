"""
Prueba de resolver_titular (familias, v2.5). No necesita base de datos ni red:
usa una "base" falsa en memoria que solo sabe hacer db.get(Modelo, id).

Correr desde la carpeta del backend:   python -m pytest tests/ -q
"""
import asyncio
import os
import uuid

# auth.py importa config.py, que exige estas variables. Valores falsos: la
# prueba no se conecta a nada (PyJWKClient recién descarga claves con un token).
for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
           "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",  # PyJWKClient exige http(s)
    }.get(_k, "x"))

from app.auth import resolver_titular
from app.models import Family, User


class BaseFalsa:
    """Imita lo único que usa resolver_titular: await db.get(Modelo, pk)."""
    def __init__(self, *objetos):
        self._por_clave = {(type(o), o.id): o for o in objetos}

    async def get(self, modelo, pk):
        return self._por_clave.get((modelo, pk))


def usuario(family_id=None):
    return User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:6]}@x.com", family_id=family_id)


def familia(titular, id_=1):
    return Family(id=id_, owner_id=titular.id)


def resolver(db, u):
    return asyncio.run(resolver_titular(db, u))


def test_sin_familia_devuelve_a_si_mismo():
    u = usuario()
    assert resolver(BaseFalsa(u), u) is u


def test_titular_devuelve_a_si_mismo():
    t = usuario(family_id=1)
    assert resolver(BaseFalsa(t, familia(t)), t) is t


def test_miembro_devuelve_al_titular():
    t = usuario(family_id=1)
    m = usuario(family_id=1)
    assert resolver(BaseFalsa(t, m, familia(t)), m) is t


def test_family_id_a_una_familia_inexistente_devuelve_a_si_mismo():
    m = usuario(family_id=99)
    assert resolver(BaseFalsa(m), m) is m


def test_titular_borrado_devuelve_a_si_mismo():
    # La familia existe pero su titular ya no está en la base.
    fantasma = usuario()
    m = usuario(family_id=1)
    assert resolver(BaseFalsa(m, familia(fantasma)), m) is m


def test_dos_familias_no_se_mezclan():
    t1, t2 = usuario(family_id=1), usuario(family_id=2)
    m1, m2 = usuario(family_id=1), usuario(family_id=2)
    db = BaseFalsa(t1, t2, m1, m2, familia(t1, 1), familia(t2, 2))
    assert resolver(db, m1) is t1
    assert resolver(db, m2) is t2
    assert resolver(db, m1) is not t2
