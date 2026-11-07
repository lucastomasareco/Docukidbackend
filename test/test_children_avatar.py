"""
Pruebas de la foto de perfil del hijo (issue #36): validación de `avatar_url`
en los formularios de crear y editar. No necesitan base de datos ni red.

Correr desde la carpeta del backend:   python -m pytest test/test_children_avatar.py -q
"""
import os

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
           "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

import pytest
from pydantic import ValidationError

from app.schemas import AVATAR_MAX_CARACTERES, ChildCreate, ChildOut, ChildUpdate

FOTO = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD="


def test_crear_sin_foto_sigue_funcionando():
    assert ChildCreate(name="Ana").avatar_url is None


def test_crear_con_foto_valida():
    assert ChildCreate(name="Ana", avatar_url=FOTO).avatar_url == FOTO


@pytest.mark.parametrize("formato", ["jpeg", "png", "webp"])
def test_formatos_permitidos(formato):
    assert ChildCreate(name="Ana", avatar_url=f"data:image/{formato};base64,AAAA").avatar_url


@pytest.mark.parametrize("malo", [
    "https://ejemplo.com/foto.jpg",         # no es data URI
    "data:image/gif;base64,AAAA",           # formato no permitido
    "data:text/html;base64,AAAA",           # no es imagen
    "data:image/png;base64,",               # vacía
    "data:image/png;base64,AA AA<script>",  # caracteres no válidos
    "",
])
def test_rechaza_fotos_invalidas(malo):
    with pytest.raises(ValidationError):
        ChildCreate(name="Ana", avatar_url=malo)


def test_rechaza_foto_demasiado_pesada():
    enorme = "data:image/png;base64," + "A" * AVATAR_MAX_CARACTERES
    with pytest.raises(ValidationError):
        ChildCreate(name="Ana", avatar_url=enorme)


def test_editar_solo_la_foto_es_valido():
    cambios = ChildUpdate(avatar_url=FOTO)
    assert cambios.model_dump(exclude_unset=True) == {"avatar_url": FOTO}


def test_editar_quitar_la_foto_con_null():
    cambios = ChildUpdate(avatar_url=None)
    assert cambios.model_dump(exclude_unset=True) == {"avatar_url": None}


def test_editar_cuerpo_vacio_sigue_rechazado():
    with pytest.raises(ValidationError):
        ChildUpdate()


def test_editar_solo_nombre_no_toca_la_foto():
    assert "avatar_url" not in ChildUpdate(name="Luz").model_dump(exclude_unset=True)


def test_salida_incluye_avatar_url():
    class Fila:  # imita al modelo de SQLAlchemy
        id, name, birth_date, avatar_url = 1, "Ana", None, FOTO
    assert ChildOut.model_validate(Fila()).avatar_url == FOTO
