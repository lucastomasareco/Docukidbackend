"""
Pruebas del lugar del turno (v2.7). No necesitan base de datos ni red:
Nominatim se reemplaza por una función falsa.

Correr desde la carpeta del backend:   python -m pytest test/ -q
"""
import asyncio
import os

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
           "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import places
from app.calendar_sync import texto_de_lugar
from app.schemas import AppointmentCreate, AppointmentUpdate


def crear(**extra):
    return AppointmentCreate(child_id=1, title="Pediatra", date="2026-12-01", **extra)


# ---------- Coordenadas ----------
def test_sin_lugar_es_valido():
    t = crear()
    assert t.latitude is None and t.longitude is None


def test_lugar_completo_es_valido():
    t = crear(location_name="Dr. Pérez", address="Av. X 1", latitude=-34.6, longitude=-58.38)
    assert t.latitude == -34.6


def test_solo_latitud_se_rechaza():
    with pytest.raises(ValidationError):
        crear(latitude=-34.6)


def test_solo_longitud_se_rechaza():
    with pytest.raises(ValidationError):
        crear(longitude=-58.3)


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_fuera_de_rango_se_rechaza(lat, lon):
    with pytest.raises(ValidationError):
        crear(latitude=lat, longitude=lon)


def test_limites_exactos_son_validos():
    crear(latitude=90, longitude=180)
    crear(latitude=-90, longitude=-180)


def test_patch_quitar_lugar_con_los_cuatro_en_null():
    t = AppointmentUpdate(location_name=None, address=None, latitude=None, longitude=None)
    assert t.model_dump(exclude_unset=True) == {
        "location_name": None, "address": None, "latitude": None, "longitude": None}


def test_patch_solo_una_coordenada_se_rechaza():
    with pytest.raises(ValidationError):
        AppointmentUpdate(latitude=-34.6)


def test_patch_solo_nombre_del_lugar_es_valido():
    assert AppointmentUpdate(location_name="Otro nombre").location_name == "Otro nombre"


def test_patch_vacio_se_sigue_rechazando():
    with pytest.raises(ValidationError):
        AppointmentUpdate()


# ---------- Texto para Google Calendar ----------
def test_texto_de_lugar():
    assert texto_de_lugar("Dr. Pérez", "Av. X 1") == "Dr. Pérez, Av. X 1"
    assert texto_de_lugar("Dr. Pérez", None) == "Dr. Pérez"
    assert texto_de_lugar(None, " Av. X 1 ") == "Av. X 1"
    assert texto_de_lugar("  ", "") is None
    assert texto_de_lugar(None, None) is None


# ---------- Respuesta de lugares ----------
def test_direccion_corta():
    a = {"road": "Avenida Corrientes", "house_number": "1234", "city": "Buenos Aires", "state": "CABA"}
    assert places.direccion_corta(a, "largo") == "Avenida Corrientes 1234, Buenos Aires, CABA"
    assert places.direccion_corta(None, "Texto largo") == "Texto largo"
    assert places.direccion_corta({}, None) is None


def test_busqueda_arma_resultados_y_usa_cache(monkeypatch):
    llamadas = []

    async def falsa(ruta, params):
        llamadas.append(ruta)
        return [
            {"name": "Clínica X", "lat": "-34.6", "lon": "-58.4", "display_name": "Clínica X, BA",
             "address": {"road": "Calle 1", "city": "Avellaneda"}},
            {"lat": "malo", "lon": "1"},  # se descarta
        ]

    monkeypatch.setattr(places, "_pedir_a_nominatim", falsa)
    places._cache.clear()
    r1 = asyncio.run(places.buscar_lugar(q="clinica   X"))
    r2 = asyncio.run(places.buscar_lugar(q="Clinica X"))
    assert r1["results"] == [{"name": "Clínica X", "address": "Calle 1, Avellaneda", "lat": -34.6, "lon": -58.4}]
    assert r2 == r1
    assert len(llamadas) == 1  # la segunda salió de la caché


def test_reverse_sin_direccion_no_es_error(monkeypatch):
    async def falsa(ruta, params):
        return {"error": "Unable to geocode"}

    monkeypatch.setattr(places, "_pedir_a_nominatim", falsa)
    places._cache.clear()
    r = asyncio.run(places.dirigir_punto(lat=0.0, lon=0.0))
    assert r == {"name": None, "address": None, "lat": 0.0, "lon": 0.0}


def test_reverse_con_direccion(monkeypatch):
    async def falsa(ruta, params):
        return {"name": "", "lat": "1", "lon": "2", "display_name": "x",
                "address": {"road": "Calle 9", "house_number": "5", "town": "Wilde"}}

    monkeypatch.setattr(places, "_pedir_a_nominatim", falsa)
    places._cache.clear()
    r = asyncio.run(places.dirigir_punto(lat=-34.7, lon=-58.3))
    assert r["address"] == "Calle 9 5, Wilde" and r["name"] is None and r["lat"] == -34.7


def test_fallo_de_nominatim_da_502(monkeypatch):
    import httpx

    class Cliente:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise httpx.ConnectTimeout("tarda")

    monkeypatch.setattr(places.httpx, "AsyncClient", Cliente)
    monkeypatch.setattr(places, "INTERVALO_MINIMO", 0)
    with pytest.raises(HTTPException) as e:
        asyncio.run(places._pedir_a_nominatim("/search", {}))
    assert e.value.status_code == 502 and "mapa" in e.value.detail
