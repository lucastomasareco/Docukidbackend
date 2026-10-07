"""
schemas.py
----------
Estas clases NO son las tablas de la base de datos (eso está en models.py).
Son los "formularios" que definen exactamente qué forma tiene que tener la
información que entra a cada endpoint (request) y qué forma tiene la que sale
(response). FastAPI las usa para validar automáticamente: si el frontend manda
algo mal formado, FastAPI responde con un error 422 claro, sin que tengamos
que escribir ese chequeo a mano.
"""

from datetime import date
from datetime import time as dt_time
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# ---------- Hijos (children) ----------

class ChildCreate(BaseModel):
    name: str
    birth_date: Optional[date] = None


class ChildUpdate(BaseModel):
    name: str


class ChildOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # permite construirlo desde el modelo de SQLAlchemy

    id: int
    name: str
    birth_date: Optional[date] = None


class ChildrenResponse(BaseModel):
    children: List[ChildOut]


class ChildResponse(BaseModel):
    child: ChildOut


# ---------- Documentos ----------

class UploadResponse(BaseModel):
    status: str
    expiry_date: Optional[date] = None
    drive_link: Optional[str] = None
    doc_id: int


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    expiry_date: Optional[date] = None
    status: str
    drive_link: Optional[str] = None


class DocumentsResponse(BaseModel):
    documents: List[DocumentOut]


class DeleteResponse(BaseModel):
    message: str


# ---------- Turnos (appointments) ----------

class AppointmentCreate(BaseModel):
    child_id: int
    title: str
    date: date
    time: Optional[dt_time] = None
    notes: Optional[str] = None


class AppointmentCreatedResponse(BaseModel):
    ok: bool
    calendar_event_id: str


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    date: date
    time: Optional[dt_time] = None
    notes: Optional[str] = None
    calendar_event_id: Optional[str] = None


class AppointmentsResponse(BaseModel):
    appointments: List[AppointmentOut]
