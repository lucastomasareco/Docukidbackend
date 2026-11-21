"""
places.py
---------
Lugar del turno (v2.7): busca un lugar y averigua la dirección de un punto del mapa
usando Nominatim, el buscador de OpenStreetMap. La app NO llama a Nominatim directo:
pasa por acá para (1) evitar problemas de CORS, (2) mandar un User-Agent propio (Nominatim
bloquea a quien no se identifica) y (3) cumplir su regla de 1 consulta por segundo.

  GET /places/search?q=...        -> {"results": [{"name", "address", "lat", "lon"}, ...]}  (hasta 5)
  GET /places/reverse?lat=&lon=   -> {"name", "address", "lat", "lon"}

Ambos piden sesión (cualquier persona logueada, también los miembros de una familia).
No usan API key ni variables nuevas en Render.

Límites de Nominatim: https://operations.osmfoundation.org/policies/nominatim/
Alcanza de sobra para la demo; si algún día se usa a gran escala hay que cambiar de proveedor.
"""

import asyncio
import logging
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from .auth import get_current_user

logger = logging.getLogger(__name__)

# Nominatim exige identificar la aplicación. Si cambia el repositorio o el contacto, se cambia acá.
USER_AGENT = "Docukids/2.7 (proyecto universitario; https://github.com/lucastomasareco/docukidbackend)"
NOMINATIM = "https://nominatim.openstreetmap.org"
TIMEOUT_SEGUNDOS = 8.0
INTERVALO_MINIMO = 1.1  # segundos entre consultas a Nominatim (su límite es 1 por segundo)
CACHE_SEGUNDOS = 60 * 60
CACHE_MAXIMO = 500

router = APIRouter(prefix="/places", tags=["places"], dependencies=[Depends(get_current_user)])

# ---------- Caché en memoria (se pierde al reiniciar el servidor; no pasa nada) ----------
_cache: dict[str, tuple[float, Any]] = {}


def _cache_leer(clave: str) -> Optional[Any]:
    hallado = _cache.get(clave)
    if hallado is None:
        return None
    vence, valor = hallado
    if time.monotonic() > vence:
        _cache.pop(clave, None)
        return None
    return valor


def _cache_guardar(clave: str, valor: Any) -> None:
    if len(_cache) >= CACHE_MAXIMO:
        _cache.pop(next(iter(_cache)))  # saca la entrada más vieja
    _cache[clave] = (time.monotonic() + CACHE_SEGUNDOS, valor)


# ---------- Límite de 1 consulta por segundo hacia Nominatim ----------
_candado = asyncio.Lock()
_ultima_consulta = 0.0


async def _pedir_a_nominatim(ruta: str, params: dict) -> Any:
    """Hace UNA consulta a Nominatim respetando el intervalo mínimo. Traduce los fallos a HTTPException."""
    global _ultima_consulta
    async with _candado:
        espera = INTERVALO_MINIMO - (time.monotonic() - _ultima_consulta)
        if espera > 0:
            await asyncio.sleep(espera)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
                respuesta = await cliente.get(
                    f"{NOMINATIM}{ruta}",
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "es"},
                )
        except httpx.HTTPError as e:
            logger.warning("Nominatim no respondió (%s): %s", ruta, e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="No pudimos buscar ahora. Tocá el mapa para marcar el lugar.",
            )
        finally:
            _ultima_consulta = time.monotonic()

    if respuesta.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="El buscador está ocupado. Probá de nuevo en unos segundos o tocá el mapa.",
        )
    if respuesta.status_code != 200:
        logger.warning("Nominatim devolvió HTTP %s (%s)", respuesta.status_code, ruta)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No pudimos buscar ahora. Tocá el mapa para marcar el lugar.",
        )
    try:
        return respuesta.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No pudimos buscar ahora. Tocá el mapa para marcar el lugar.",
        )


# ---------- Armado de la respuesta ----------
def direccion_corta(address: Optional[dict], display_name: Optional[str]) -> Optional[str]:
    """'Av. Corrientes 1234, Buenos Aires' en vez del texto larguísimo de Nominatim."""
    a = address or {}
    calle = a.get("road") or a.get("pedestrian") or a.get("footway") or a.get("path")
    if calle and a.get("house_number"):
        calle = f"{calle} {a['house_number']}"
    zona = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("suburb")
    partes: list[str] = []
    for p in (calle, zona, a.get("state")):
        if p and p not in partes:
            partes.append(p)
    if partes:
        return ", ".join(partes)
    return (display_name or "").strip() or None


def _a_float(valor: Any) -> Optional[float]:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _resultado(item: dict) -> Optional[dict]:
    lat, lon = _a_float(item.get("lat")), _a_float(item.get("lon"))
    if lat is None or lon is None:
        return None
    nombre = (item.get("name") or "").strip() or None
    return {
        "name": nombre,
        "address": direccion_corta(item.get("address"), item.get("display_name")),
        "lat": lat,
        "lon": lon,
    }


# ---------- Endpoints ----------
@router.get("/search")
async def buscar_lugar(q: str = Query(..., min_length=3, max_length=200)):
    texto = " ".join(q.split())
    clave = f"s:{texto.lower()}"
    guardado = _cache_leer(clave)
    if guardado is not None:
        return guardado

    datos = await _pedir_a_nominatim(
        "/search",
        {"q": texto, "format": "jsonv2", "limit": 5, "addressdetails": 1},
    )
    resultados = [r for r in (_resultado(i) for i in datos if isinstance(i, dict)) if r] if isinstance(datos, list) else []
    respuesta = {"results": resultados}
    _cache_guardar(clave, respuesta)
    return respuesta


@router.get("/reverse")
async def dirigir_punto(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    clave = f"r:{lat:.5f},{lon:.5f}"
    guardado = _cache_leer(clave)
    if guardado is not None:
        return guardado

    datos = await _pedir_a_nominatim(
        "/reverse",
        {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18, "addressdetails": 1},
    )
    # Si no hay nada en ese punto (mar, campo), Nominatim devuelve {"error": ...}: no es un fallo,
    # simplemente no hay dirección. Se responde 200 con la dirección vacía.
    if isinstance(datos, dict) and "error" not in datos:
        item = _resultado({**datos, "lat": lat, "lon": lon})
    else:
        item = None
    respuesta = item or {"name": None, "address": None, "lat": lat, "lon": lon}
    _cache_guardar(clave, respuesta)
    return respuesta
