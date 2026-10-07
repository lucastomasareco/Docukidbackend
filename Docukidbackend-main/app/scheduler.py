"""
scheduler.py
------------
Fase 2, paso 6 (último paso de la Fase 2): expone POST /scheduler/check, el
endpoint que cron-job.org llama una vez al día (Guía Técnica, sección 4 y
"Plan de construcción", Fase 3). Recorre los documentos que están por vencer
(o que ya vencieron) y todavía no fueron notificados, les manda el correo
con notifier.py, y marca cada uno como notificado SOLO si el envío salió bien.

Protección (Guía Técnica, sección 4): cabecera X-Cron-Secret, comparada
contra la variable de entorno CRON_SECRET. Si no coincide (o falta), 401 y
no se hace nada más -- así nadie más puede disparar el envío masivo.

Efecto colateral documentado en la Guía Técnica (sección 1): esta misma
consulta diaria a la base de datos es lo que evita que Supabase pause el
proyecto gratuito por 7 días de inactividad. No es un truco aparte: es
gratis porque el scheduler ya tenía que existir de todas formas.

Nota sobre fechas: NO importamos `date` ni `ZoneInfo` acá. Todos los cálculos
de "hoy" pasan por `crud.hoy()`, que es la ÚNICA fuente de verdad para la
fecha actual en el proyecto (ver crud.py). Así, si mañana cambia la zona
horaria vía APP_TIMEZONE, este archivo no necesita ni enterarse.
"""

import asyncio

from aiolimiter import AsyncLimiter  # <-- NUEVO: rate limiter async real
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from . import crud
from .config import settings
from .crud import hoy  # <-- Helper centralizado: única fuente de verdad de "hoy"
from .database import get_db
from .notifier import enviar_aviso_vencimiento

router = APIRouter(tags=["scheduler"])

CRON_SECRET = settings.cron_secret

# --- Decisión NUEVA (Docukids.md sí menciona el ejemplo "faltan 15 días",
# pero no lo fija como una constante configurable; lo hacemos configurable
# por si el número cambia más adelante) ---
UMBRAL_DIAS_AVISO = settings.notification_threshold_days

# --- LÍMITE DE CONCURRENCIA ---
# Máximo de envíos de correo simultáneos en el threadpool. Protege contra
# saturar el threadpool de Starlette si algún día hay cientos de documentos.
MAX_CONCURRENT_EMAILS = 5

# --- RATE LIMITER PARA GMAIL ---
# 1 envío por segundo, global para todo el proceso. Resuelve el rate limit
# de la API de Gmail serializando y espaciando los envíos reales: aunque el
# semáforo deje pasar 5 tareas en paralelo al threadpool, solo 1 por segundo
# llega efectivamente a la red. Es a nivel de módulo porque el límite es del
# proceso, no de una request puntual.
EMAIL_RATE_LIMITER = AsyncLimiter(max_rate=1, time_period=1)


async def _procesar_envio(documento, hijo, usuario, semaphore: asyncio.Semaphore):
    """
    Envía el aviso de UN documento y devuelve el documento si salió bien,
    o None si falló (en cuyo caso NO se debe marcar como notificado).

    El semáforo limita cuántos envíos corren a la vez en el threadpool.
    El rate limiter limita cuántos por segundo llegan realmente a Gmail.
    Son dos límites con propósitos distintos y se aplican en ese orden.
    """
    async with semaphore:
        # FIX: usamos el helper centralizado. Ya no hay lógica de timezone
        # inline acá; la referencia temporal es exactamente la misma que usa
        # compute_status(), get_documentos_pendientes_de_aviso() y
        # marcar_documento_notificado().
        dias_restantes = (documento.expiry_date - hoy()).days
        try:
            # El rate limiter envuelve la llamada real a Gmail. El semáforo
            # ya nos dejó entrar al threadpool, pero acá esperamos nuestro
            # turno de "1 por segundo" antes de disparar la petición HTTP.
            async with EMAIL_RATE_LIMITER:
                # Envío de correo: es red bloqueante, va al threadpool para
                # no congelar el Event Loop mientras esperamos a Gmail.
                await run_in_threadpool(
                    enviar_aviso_vencimiento,
                    user=usuario,
                    nombre_hijo=hijo.name,
                    nombre_documento=documento.name,
                    fecha_vencimiento=documento.expiry_date,
                    dias_restantes=dias_restantes,
                )
            return documento
        except Exception:
            # No marcamos last_notified_at: este documento se vuelve a
            # intentar mañana. Tampoco frenamos el resto del lote por un
            # usuario puntual que, por ejemplo, revocó el permiso de Google.
            return None


@router.post("/scheduler/check")
async def revisar_vencimientos(
    x_cron_secret: str = Header(default=None, alias="X-Cron-Secret"),
    db: AsyncSession = Depends(get_db),
):
    if not CRON_SECRET:
        raise HTTPException(status_code=500, detail="Falta CRON_SECRET en el .env del servidor")

    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="X-Cron-Secret inválido o ausente")

    filas = await crud.get_documentos_pendientes_de_aviso(db, UMBRAL_DIAS_AVISO)
    if not filas:
        return {"processed": 0}

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EMAILS)

    # Lanzamos TODOS los envíos de correo en paralelo. El semáforo limita
    # el threadpool (máx. 5 a la vez) y EMAIL_RATE_LIMITER espacia las
    # llamadas reales a la API de Gmail a 1 por segundo. Si mañana hay 500
    # documentos, esto no revienta ni el threadpool ni el rate limit de Google.
    tasks = [
        _procesar_envio(documento, hijo, usuario, semaphore)
        for documento, hijo, usuario in filas
    ]
    resultados = await asyncio.gather(*tasks)

    # Actualizamos la base de datos de forma SECUENCIAL. Importante: una
    # AsyncSession de SQLAlchemy NO es segura para uso concurrente, así que
    # los UPDATEs se hacen uno por uno, después de que terminaron todas las
    # tareas de red.
    procesados = 0
    for documento in resultados:
        if documento is not None:
            await crud.marcar_documento_notificado(db, documento)
            procesados += 1

    return {"processed": procesados}