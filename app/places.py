"""
places.py
---------
Buscador de lugares para el turno (v2.7). Consulta Nominatim (el buscador de
OpenStreetMap) DESDE EL BACKEND, por tres motivos:
  - el navegador no tiene problemas de CORS con Nominatim;
  - Nominatim exige un User-Agent propio que identifique a la aplicación;
  - Nominatim permite como máximo 1 consulta por segundo: acá se controla para
    toda la app a la vez, no por cada pestaña.

Endpoints (requieren sesión; cualquier miembro de la familia puede buscar):
  GET /places/search?q=...        -> {results: [{name, address, lat, lon}]}
  GET /places/reverse?lat=&lon=   -> {name, address, lat, lon}  (al tocar el mapa)

No hay API key ni variables obligatorias nuevas. Política de uso:
https://operations.osmfoundation.org/policies/nominatim/
"""

import asyncio
import logging
import time
from typing import List, Optional

import httpx
from aiolimiter import AsyncLimiter
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .auth import get_current_user
from .config import settings
from .models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/places", tags=["places"])

NOMINATIM_URL = "https://nominatim.openstreetmap.org"
TIMEOUT_SEGUNDOS = 8
# Máximo 1 consulta por segundo hacia Nominatim, compartido por todos los usuarios.
_limitador = AsyncLimiter(1, 1.1)
ESPERA_MAXIMA_TURNO = 6  # si la cola es más larga que esto, se responde "probá de nuevo"

# Caché en memoria: evita repetir la misma consulta (y gastar el límite).
CACHE_SEGUNDOS = 600
CACHE_MAX = 200
_cache: dict = {}


class PlaceOut(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    lat: float
    lon: float


class PlaceSearchResponse(BaseModel):
    results: List[PlaceOut]


class PlaceReverseResponse(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    lat: float
    lon: float


def normalizar_resultado(item: dict) -> Optional[PlaceOut]:
    """Convierte un resultado crudo de Nominatim al formato simple de la app (o None si no sirve)."""
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    direccion = (item.get("display_name") or "").strip() or None
    nombre = (item.get("name") or "").strip() or None
    if not nombre and direccion:
        nombre = direccion.split(",")[0].strip() or None
    return PlaceOut(name=nombre, address=direccion, lat=lat, lon=lon)


def _cache_get(clave):
    entrada = _cache.get(clave)
    if entrada and time.monotonic() - entrada[0] < CACHE_SEGUNDOS:
        return entrada[1]
    _cache.pop(clave, None)
    return None


def _cache_set(clave, valor) -> None:
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)  # descarta la entrada más vieja
    _cache[clave] = (time.monotonic(), valor)


async def _consultar_nominatim(ruta: str, params: dict):
    """Una consulta a Nominatim respetando el límite. Traduce los fallos a errores claros."""
    try:
        await asyncio.wait_for(_limitador.acquire(), timeout=ESPERA_MAXIMA_TURNO)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="El buscador de lugares está ocupado. Probá de nuevo en unos segundos.",
        )
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
            respuesta = await cliente.get(
                f"{NOMINATIM_URL}{ruta}",
                params=params,
                headers={"User-Agent": settings.nominatim_user_agent, "Accept-Language": "es"},
            )
        respuesta.raise_for_status()
        return respuesta.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Nominatim falló en %s: %s", ruta, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No pudimos consultar el buscador de lugares ahora. Podés tocar el mapa para marcar el lugar.",
        )


@router.get("/search", response_model=PlaceSearchResponse)
async def buscar_lugares(
    q: str = Query(..., min_length=3, max_length=200),
    usuario: User = Depends(get_current_user),
):
    consulta = " ".join(q.split())
    if len(consulta) < 3:
        raise HTTPException(status_code=422, detail="Escribí al menos 3 letras para buscar.")
    clave = ("search", consulta.lower())
    en_cache = _cache_get(clave)
    if en_cache is not None:
        return en_cache

    crudos = await _consultar_nominatim(
        "/search", {"q": consulta, "format": "jsonv2", "limit": 5, "addressdetails": 0}
    )
    resultados = [r for r in (normalizar_resultado(i) for i in crudos) if r is not None]
    respuesta = PlaceSearchResponse(results=resultados)
    _cache_set(clave, respuesta)
    return respuesta


@router.get("/reverse", response_model=PlaceReverseResponse)
async def lugar_de_un_punto(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    usuario: User = Depends(get_current_user),
):
    clave = ("reverse", round(lat, 5), round(lon, 5))
    en_cache = _cache_get(clave)
    if en_cache is not None:
        return en_cache

    crudo = await _consultar_nominatim(
        "/reverse", {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18}
    )
    # Si el punto no tiene dirección (mar, campo), Nominatim responde {"error": ...}:
    # no es un fallo, solo no hay texto para mostrar. Las coordenadas son las que tocó la persona.
    lugar = normalizar_resultado({**crudo, "lat": lat, "lon": lon}) if isinstance(crudo, dict) and "error" not in crudo else None
    respuesta = PlaceReverseResponse(
        name=lugar.name if lugar else None,
        address=lugar.address if lugar else None,
        lat=lat,
        lon=lon,
    )
    _cache_set(clave, respuesta)
    return respuesta
