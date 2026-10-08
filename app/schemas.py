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
from datetime import date as date_type  # alias para no pisar el campo "date" dentro de las clases
from datetime import time as dt_time
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


# ---------- Hijos (children) ----------

class ChildCreate(BaseModel):
    name: str
    birth_date: Optional[date] = None


class ChildUpdate(BaseModel):
    """
    Cuerpo de PUT /children/{child_id} (editar un hijo).
    Se manda SOLO lo que cambia; lo que no se manda, queda como estaba.
      - name: no puede ser vacío ni null.
      - birth_date: "AAAA-MM-DD" para fijarla; null para borrarla.
    Un cuerpo vacío ({}) se rechaza con 422.
    Compatible con la app actual, que manda solo { name }.
    """
    name: Optional[str] = None
    birth_date: Optional[date] = None

    @model_validator(mode="after")
    def _validar_cambios(self):
        enviados = self.model_fields_set
        if not enviados:
            raise ValueError("Mandá al menos un campo para cambiar (name o birth_date).")
        if "name" in enviados and (self.name is None or not self.name.strip()):
            raise ValueError("El nombre no puede estar vacío.")
        return self


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
    Cuerpo de PATCH /documents/{doc_id}. Se manda SOLO lo que cambia, con al
    menos una de estas cosas:
      { "name": "DNI de Lola" }         -> cambia el nombre (no puede ser vacío)
      { "expiry_date": "2027-03-15" }   -> fija la fecha (no_expiry pasa a false)
      { "no_expiry": true }             -> el documento no vence (expiry_date pasa a NULL)
    El nombre se puede combinar con una de las dos formas de vencimiento.
    Se rechazan con 422: cuerpo vacío, expiry_date y no_expiry=true juntos,
    no_expiry=false, y un nombre vacío o null.
    """
    name: Optional[str] = None
    expiry_date: Optional[date] = None
    no_expiry: Optional[bool] = None

    @model_validator(mode="after")
    def _validar(self):
        if "name" in self.model_fields_set:
            nombre = (self.name or "").strip()
            if not nombre:
                raise ValueError("El nombre no puede estar vacío.")
            self.name = nombre
        if self.no_expiry is False:
            raise ValueError("no_expiry solo puede ser true; para fijar una fecha mandá expiry_date.")
        if self.no_expiry is True and self.expiry_date is not None:
            raise ValueError("Mandá expiry_date o no_expiry=true, no las dos a la vez.")
        if self.name is None and self.expiry_date is None and self.no_expiry is None:
            raise ValueError("Mandá al menos un campo: name, expiry_date o no_expiry=true.")
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


class AppointmentUpdate(BaseModel):
    """
    Cuerpo de PATCH /appointments/{appointment_id} (editar un turno).
    Se manda SOLO lo que cambia; lo que no se manda, queda como estaba.
      - title: no puede ser vacío ni null.
      - date: no puede ser null.
      - time: "HH:MM" para fijar hora; null para quitarla (el evento pasa a ser de todo el día).
      - notes: texto, o null / "" para borrarlas.
    Un cuerpo vacío ({}) se rechaza con 422.
    """
    title: Optional[str] = None
    date: Optional[date_type] = None
    time: Optional[dt_time] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _validar_cambios(self):
        enviados = self.model_fields_set
        if not enviados:
            raise ValueError("Mandá al menos un campo para cambiar (title, date, time o notes).")
        if "title" in enviados and (self.title is None or not self.title.strip()):
            raise ValueError("El título no puede estar vacío.")
        if "date" in enviados and self.date is None:
            raise ValueError("La fecha no puede ser null.")
        return self


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
