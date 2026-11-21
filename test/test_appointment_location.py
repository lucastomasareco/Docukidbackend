"""
Pruebas del lugar del turno (v2.7): validación de los formularios, texto del
evento de Google Calendar y limpieza de resultados de Nominatim. No necesitan
base de datos ni red.

Correr desde la carpeta del backend:   python -m pytest test/test_appointment_location.py -q
"""
import os

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
           "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

from datetime import date, time

import pytest
from pydantic import ValidationError

from app.calendar_sync import _armar_cuerpo_evento, texto_de_lugar
from app.places import normalizar_resultado
from app.schemas import AppointmentCreate, AppointmentOut, AppointmentUpdate

BASE = dict(child_id=1, title="Pediatra", date=date(2026, 12, 1))


# ---------- crear ----------
def test_crear_sin_lugar_sigue_funcionando():
    t = AppointmentCreate(**BASE)
    assert t.latitude is None and t.longitude is None and t.address is None


def test_crear_con_lugar_completo():
    t = AppointmentCreate(**BASE, location_name="  Dr. Pérez ", address="Av. Siempre Viva 742",
                          latitude=-34.6, longitude=-58.38)
    assert t.location_name == "Dr. Pérez"
    assert t.latitude == -34.6


def test_crear_con_solo_direccion_es_valido():
    assert AppointmentCreate(**BASE, address="Calle 1").latitude is None


def test_texto_vacio_pasa_a_none():
    assert AppointmentCreate(**BASE, location_name="   ").location_name is None


@pytest.mark.parametrize("lat,lon", [(10, None), (None, 10)])
def test_crear_rechaza_coordenada_suelta(lat, lon):
    with pytest.raises(ValidationError):
        AppointmentCreate(**BASE, latitude=lat, longitude=lon)


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_crear_rechaza_fuera_de_rango(lat, lon):
    with pytest.raises(ValidationError):
        AppointmentCreate(**BASE, latitude=lat, longitude=lon)


def test_crear_acepta_los_bordes_y_el_cero():
    AppointmentCreate(**BASE, latitude=0, longitude=0)
    AppointmentCreate(**BASE, latitude=90, longitude=-180)


def test_crear_rechaza_texto_demasiado_largo():
    with pytest.raises(ValidationError):
        AppointmentCreate(**BASE, address="x" * 301)


# ---------- editar (PATCH) ----------
def test_patch_solo_titulo_no_toca_el_lugar():
    u = AppointmentUpdate(title="Otro")
    assert "latitude" not in u.model_fields_set


def test_patch_mover_el_pin():
    u = AppointmentUpdate(latitude=-34.6, longitude=-58.4, address="Nueva")
    assert u.model_dump(exclude_unset=True)["latitude"] == -34.6


def test_patch_quitar_el_lugar_con_cuatro_null():
    u = AppointmentUpdate(location_name=None, address=None, latitude=None, longitude=None)
    assert u.model_dump(exclude_unset=True) == {
        "location_name": None, "address": None, "latitude": None, "longitude": None}


def test_patch_rechaza_una_sola_coordenada():
    with pytest.raises(ValidationError):
        AppointmentUpdate(latitude=10)


def test_patch_solo_lugar_ya_no_es_cuerpo_vacio():
    AppointmentUpdate(location_name="Clínica")


def test_patch_vacio_se_sigue_rechazando():
    with pytest.raises(ValidationError):
        AppointmentUpdate()


# ---------- salida ----------
def test_salida_incluye_el_lugar_y_turnos_viejos_lo_tienen_vacio():
    class Fila:
        id, title, date, time, notes, calendar_event_id = 1, "x", date(2026, 1, 1), None, None, "e"
        location_name, address, latitude, longitude = None, None, None, None
    out = AppointmentOut.model_validate(Fila())
    assert out.latitude is None and out.location_name is None


# ---------- Google Calendar ----------
def test_texto_de_lugar():
    assert texto_de_lugar("Clínica Sol", "Av. 1, Ciudad") == "Clínica Sol, Av. 1, Ciudad"
    assert texto_de_lugar("Clínica Sol", "Clínica Sol, Av. 1") == "Clínica Sol, Av. 1"
    assert texto_de_lugar(None, "Av. 1") == "Av. 1"
    assert texto_de_lugar("Clínica Sol", None) == "Clínica Sol"
    assert texto_de_lugar(None, None) is None
    assert texto_de_lugar("  ", "") is None


def test_el_evento_lleva_ubicacion_solo_si_hay_lugar():
    con = _armar_cuerpo_evento("Pediatra", date(2026, 12, 1), time(10, 0), None, "Clínica Sol")
    sin = _armar_cuerpo_evento("Pediatra", date(2026, 12, 1), None, None, None)
    assert con["location"] == "Clínica Sol"
    assert "location" not in sin


# ---------- Nominatim ----------
def test_normalizar_resultado_normal():
    r = normalizar_resultado({"lat": "-34.6", "lon": "-58.4", "name": "Clínica Sol",
                              "display_name": "Clínica Sol, Av. 1, Ciudad"})
    assert (r.name, r.lat, r.lon) == ("Clínica Sol", -34.6, -58.4)


def test_normalizar_sin_name_usa_el_primer_tramo_de_la_direccion():
    r = normalizar_resultado({"lat": "1", "lon": "2", "name": "", "display_name": "Av. 1 742, Ciudad"})
    assert r.name == "Av. 1 742"


@pytest.mark.parametrize("item", [{}, {"lat": "x", "lon": "1"}, {"lat": "1"}])
def test_normalizar_descarta_resultados_rotos(item):
    assert normalizar_resultado(item) is None
