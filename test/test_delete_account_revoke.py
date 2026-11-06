"""
DELETE /account y el permiso de Google (v2.8, recuperar datos). Sin base ni red.
Regla: con "conservar Drive" NO se revoca el permiso de Google (si no, la app pierde el
acceso a la carpeta 'Docukids' vieja y no se puede recuperar); con delete_drive=true sí.
"""
import asyncio
import os
from types import SimpleNamespace

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

from app import main


def _correr(monkeypatch, delete_drive):
    anotado = {"revocados": [], "papelera": 0, "usuario_borrado": False, "auth_borrado": False}

    async def delete_user(db, u):
        anotado["usuario_borrado"] = True

    async def revocar(token):
        anotado["revocados"].append(token)

    async def borrar_auth(uid):
        anotado["auth_borrado"] = True

    def papelera(u):
        anotado["papelera"] += 1
        return 1

    monkeypatch.setattr(main.settings, "supabase_secret_key", "sb_secret_x", raising=False)
    monkeypatch.setattr(main.crud, "delete_user", delete_user)
    monkeypatch.setattr(main, "revocar_permiso_de_google", revocar)
    monkeypatch.setattr(main, "borrar_usuario_de_auth", borrar_auth)
    monkeypatch.setattr(main, "enviar_docukids_a_papelera", papelera)

    usuario = SimpleNamespace(id="u1", google_refresh_token="TOKEN", family_id=None)
    asyncio.run(main.eliminar_cuenta(delete_drive=delete_drive, usuario=usuario, db=None))
    return anotado


def test_conservar_drive_no_revoca_el_permiso_de_google(monkeypatch):
    r = _correr(monkeypatch, delete_drive=False)
    assert r["revocados"] == []                 # el permiso queda: así se pueden recuperar las carpetas
    assert r["papelera"] == 0                   # Drive no se toca
    assert r["usuario_borrado"] and r["auth_borrado"]   # la cuenta sí se elimina


def test_mandar_a_la_papelera_tampoco_revoca_el_permiso(monkeypatch):
    # v2.9: tampoco con delete_drive=true se revoca (si no, no se podría recuperar la carpeta).
    r = _correr(monkeypatch, delete_drive=True)
    assert r["revocados"] == []
    assert r["papelera"] == 1
    assert r["usuario_borrado"] and r["auth_borrado"]


def test_papelera_con_google_desconectado_da_aviso_claro_y_no_borra_nada(monkeypatch):
    from fastapi import HTTPException
    import pytest

    anotado = {"usuario_borrado": False, "auth_borrado": False}

    async def delete_user(db, u):
        anotado["usuario_borrado"] = True

    async def borrar_auth(uid):
        anotado["auth_borrado"] = True

    def papelera(u):  # lo que hace build_google_credentials cuando el token está vencido/revocado
        raise HTTPException(status_code=401, detail="No se pudo renovar el acceso a Google Drive: invalid_grant.")

    monkeypatch.setattr(main.settings, "supabase_secret_key", "sb_secret_x", raising=False)
    monkeypatch.setattr(main.crud, "delete_user", delete_user)
    monkeypatch.setattr(main, "borrar_usuario_de_auth", borrar_auth)
    monkeypatch.setattr(main, "enviar_docukids_a_papelera", papelera)

    usuario = SimpleNamespace(id="u1", google_refresh_token="TOKEN", family_id=None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.eliminar_cuenta(delete_drive=True, usuario=usuario, db=None))

    assert e.value.status_code == 502                       # no 401: la app no lo manda a "Ajustes"
    assert "Conservarlos en mi Drive" in e.value.detail     # trae la salida
    assert "renovar el acceso" not in e.value.detail        # no se confunde con "Google desconectado"
    assert not anotado["usuario_borrado"] and not anotado["auth_borrado"]   # no se borró nada
