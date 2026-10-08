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

from pydantic import BaseModel, ConfigDict, model_validator


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
    no_expiry: bool = False
    drive_link: Optional[str] = None
    doc_id: int


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    expiry_date: Optional[date] = None
    no_expiry: bool = False
    status: str
    drive_link: Optional[str] = None


class DocumentUpdate(BaseModel):
    """
    Cuerpo de PATCH /documents/{doc_id} (confirmar o corregir el vencimiento).
    Exactamente una de estas dos formas:
      { "expiry_date": "2027-03-15" }  -> fija la fecha (no_expiry pasa a false)
      { "no_expiry": true }            -> el documento no vence (expiry_date pasa a NULL)
    Cualquier otra combinación (vacío, ambos con valor, no_expiry=false solo)
    es ambigua y se rechaza con 422.
    """
    expiry_date: Optional[date] = None
    no_expiry: Optional[bool] = None

    @model_validator(mode="after")
    def _una_sola_forma(self):
        if self.no_expiry is True:
            if self.expiry_date is not None:
                raise ValueError(
                    "Mandá expiry_date o no_expiry=true, no las dos a la vez."
                )
        elif self.expiry_date is None:
            raise ValueError(
                "Mandá una expiry_date o no_expiry=true."
            )
        return self


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
