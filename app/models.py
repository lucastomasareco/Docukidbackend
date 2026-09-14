"""
models.py
---------
Estas clases son el "espejo en Python" de las tablas que ya creaste en
Supabase con el script SQL de la Guía Técnica (sección 3).

IMPORTANTE: este archivo NO crea las tablas (no llamamos a
Base.metadata.create_all en ningún lado). Las tablas ya existen porque las
creaste a mano pegando el SQL en el Editor SQL de Supabase. Este archivo solo
le explica a SQLAlchemy cómo son, para poder hacer consultas cómodas desde
Python en vez de escribir SQL a mano en cada endpoint.

Si en algún momento cambias una columna en Supabase, tenés que reflejar el
mismo cambio acá a mano.
"""

import uuid

from sqlalchemy import Column, Integer, Text, Date, Time, TIMESTAMP, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    # Este id es el MISMO id que genera Supabase Auth al registrarse
    # (viene en el token JWT como "sub"), no lo generamos nosotros.
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, unique=True, nullable=False)
    full_name = Column(Text, nullable=True)
    # Token para que el backend actúe en nombre del usuario en Drive/Calendar/Gmail.
    # Se completa en la Fase 2 (google_oauth.py). Por ahora queda en null.
    google_refresh_token = Column(Text, nullable=True)
    # Email de la cuenta de Google que el usuario conectó. Distinto de `email`
    # (que es el de Supabase Auth): el usuario podría haberse registrado con
    # un email de otro proveedor, y conectar una cuenta Gmail distinta. Se
    # guarda en el callback OAuth (google_oauth.py) para no tener que llamar
    # a people.getProfile() en cada envío de email. Es nullable porque los
    # usuarios que conectaron Google ANTES de agregar esta columna no lo
    # tienen (en ese caso el notifier hace fallback a getProfile).
    google_email = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    children = relationship("Child", back_populates="owner", cascade="all, delete-orphan")


class Child(Base):
    __tablename__ = "children"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    name = Column(Text, nullable=False)
    birth_date = Column(Date, nullable=True)
    avatar_url = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    owner = relationship("User", back_populates="children")
    documents = relationship("Document", back_populates="child", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="child", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"))
    name = Column(Text, nullable=False)          # Ej. "DNI", "Cartilla de Vacunación"
    type = Column(Text, nullable=True)            # Ej. "identidad", "salud"
    expiry_date = Column(Date, nullable=True)     # La detecta el OCR (Fase 2). Por ahora puede quedar vacía.
    drive_file_id = Column(Text, nullable=True)   # Se completa en Fase 2 (drive_upload.py)
    drive_link = Column(Text, nullable=True)      # Se completa en Fase 2 (drive_upload.py)
    upload_date = Column(Date, server_default=func.current_date())
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Columna agregada en la Fase 2, paso 5 (notifier.py), autorizada
    # explícitamente en AI_CONTEXT.md: "Los recordatorios deben enviarse una
    # sola vez por umbral, no todos los días. Si hace falta, agregar un
    # campo tipo last_notified_at." Se usa en scheduler.py (próximo paso)
    # para no reenviar el mismo aviso todos los días una vez notificado.
    #
    # ⚠️ IMPORTANTE: esta columna NO existe todavía en tu tabla de Supabase
    # (las tablas se crean a mano con el SQL de la Guía Técnica, sección 3;
    # este archivo no las crea). Antes de usar el scheduler tenés que correr
    # en el Editor SQL de Supabase:
    #
    #   ALTER TABLE documents ADD COLUMN last_notified_at DATE;
    #
    last_notified_at = Column(Date, nullable=True)

    # NOTA: adrede NO hay columna "status". Se calcula al vuelo en crud.py
    # comparando expiry_date con la fecha de hoy (ver sección 3 de la Guía Técnica).

    child = relationship("Child", back_populates="documents")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"))
    title = Column(Text, nullable=False)
    date = Column(Date, nullable=False)
    time = Column(Time, nullable=True)
    notes = Column(Text, nullable=True)
    calendar_event_id = Column(Text, nullable=True)  # Se completa en Fase 2 (calendar_sync.py)
    created_at = Column(TIMESTAMP, server_default=func.now())

    child = relationship("Child", back_populates="appointments")

    # NOTA: el modelo ya está listo porque la tabla existe desde el script SQL,
    # pero el endpoint /appointments NO se implementa todavía: según el plan de
    # construcción (Fase 2, paso 4) depende de calendar_sync.py, que viene después.