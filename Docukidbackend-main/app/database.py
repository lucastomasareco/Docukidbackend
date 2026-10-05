"""
database.py
-----------
Este archivo se encarga de UNA sola cosa: preparar la conexión con la base de
datos PostgreSQL de Supabase, usando SQLAlchemy en modo "async" (asíncrono)
con el driver asyncpg.

No necesitas entender todo el detalle técnico de SQLAlchemy para usar esto:
el resto del código simplemente va a "pedir prestada" una sesión de base de
datos a través de la función get_db().
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

from .config import settings

# Supabase entrega la cadena de conexión con el prefijo "postgresql://",
# pero el driver async que usamos (asyncpg) necesita "postgresql+asyncpg://".
# Esta línea hace ese cambio automáticamente para que no tengas que acordarte.
DATABASE_URL = settings.database_url
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# El "engine" es el objeto que sabe cómo hablar con la base de datos.
# echo=False evita que SQLAlchemy imprima cada consulta SQL en la consola
# (cámbialo a True temporalmente si algún día necesitas depurar consultas).
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"statement_cache_size": 0},  # <-- AQUÍ ESTÁ EL CAMBIO CLAVE (es un número 0, no un texto)
)

# Esta "fábrica" crea sesiones (conversaciones puntuales con la base de datos).
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Base que van a heredar todos los modelos de tablas (ver models.py).
Base = declarative_base()


async def get_db():
    """
    Dependencia de FastAPI: abre una sesión de base de datos, la entrega al
    endpoint que la pida, y la cierra sola al terminar (incluso si hubo un error).
    """
    async with AsyncSessionLocal() as session:
        yield session
