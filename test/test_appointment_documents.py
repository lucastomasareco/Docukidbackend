"""
Pruebas de los documentos vinculados a un turno: validación de los formularios y
forma de la respuesta. No necesitan base de datos ni red.

Correr desde la carpeta del backend:   python -m pytest test/test_appointment_documents.py -q
"""
import os

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
           "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas import AppointmentCreate, AppointmentOut, AppointmentUpdate, DocumentoDeTurno

BASE = dict(child_id=1, title="Pediatra", date=date(2026, 12, 1))


def test_crear_sin_documentos_sigue_funcionando():
    assert AppointmentCreate(**BASE).document_ids is None


def test_crear_con_documentos_quita_repetidos_y_conserva_el_orden():
    t = AppointmentCreate(**BASE, document_ids=[5, 3, 5, 9, 3])
    assert t.document_ids == [5, 3, 9]


def test_crear_rechaza_demasiados_documentos():
    with pytest.raises(ValidationError):
        AppointmentCreate(**BASE, document_ids=list(range(21)))


def test_crear_rechaza_ids_que_no_son_numeros():
    with pytest.raises(ValidationError):
        AppointmentCreate(**BASE, document_ids=["abc"])


def test_editar_sin_mandar_documentos_NO_los_toca():
    # Clave: si document_ids no viene, no puede figurar como "enviado" (borraría los vínculos).
    u = AppointmentUpdate(title="Otro título")
    assert "document_ids" not in u.model_fields_set
    assert "document_ids" not in u.model_dump(exclude_unset=True)


def test_editar_con_lista_vacia_desvincula_todos():
    u = AppointmentUpdate(document_ids=[])
    assert u.model_dump(exclude_unset=True)["document_ids"] == []


def test_editar_con_null_tambien_desvincula():
    u = AppointmentUpdate(document_ids=None)
    assert "document_ids" in u.model_fields_set
    assert u.model_dump(exclude_unset=True)["document_ids"] is None


def test_editar_solo_documentos_es_un_cuerpo_valido():
    assert AppointmentUpdate(document_ids=[1, 2]).document_ids == [1, 2]


def test_editar_cuerpo_vacio_se_sigue_rechazando():
    with pytest.raises(ValidationError):
        AppointmentUpdate()


def test_respuesta_de_un_turno_sin_documentos_trae_lista_vacia():
    turno = SimpleNamespace(id=1, title="Pediatra", date=date(2026, 12, 1), time=None, notes=None,
                            calendar_event_id=None, location_name=None, address=None,
                            latitude=None, longitude=None)
    assert AppointmentOut.model_validate(turno).documents == []


def test_documento_de_turno_se_arma_desde_el_documento():
    doc = SimpleNamespace(id=7, name="Análisis de sangre", drive_link="https://drive/x", mime_type="application/pdf",
                          expiry_date=None)
    d = DocumentoDeTurno.model_validate(doc)
    assert (d.id, d.name, d.drive_link) == (7, "Análisis de sangre", "https://drive/x")
