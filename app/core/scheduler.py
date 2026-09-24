"""Disparador periódico del motor de alertas (APScheduler, solo en local).

APScheduler 3.x ya estaba fijado en `requirements.txt`; aquí se usa su
`AsyncIOScheduler`, que corre sobre el mismo event loop que FastAPI.

Este módulo es **solo fontanería**: no decide nada de negocio, solo abre una
sesión por ciclo y llama a `app.services.motor_alertas`. En producción (Hito 7,
Lambda) se sustituye por EventBridge Scheduler invocando ese mismo servicio, y
este archivo no se despliega.

**Una sesión nueva por ciclo**, no una compartida: con `NullPool` (ver
`app/database.py`) cada sesión abre y cierra su conexión, que es lo correcto
tanto aquí como en Lambda.

**Aviso de multi-worker**: con varios workers de uvicorn, cada proceso
arrancaría su propio scheduler y las alertas se evaluarían tantas veces como
workers haya. Hoy se corre con un worker, y en Lambda + EventBridge el
problema desaparece porque el disparo es externo. Si algún día hace falta
servir con varios workers antes del Hito 7, lo barato es envolver el ciclo en
un `pg_try_advisory_lock` y salir si no se consigue.
"""

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.database import AsyncSessionLocal
from app.services.motor_alertas import procesar_alertas_pendientes
from app.services.sistema import id_usuario_sistema

logger = logging.getLogger(__name__)

JOB_MOTOR_ALERTAS = "motor-alertas"


async def ciclo_motor_alertas() -> None:
    """Un ciclo completo, con su propia sesión. Nunca deja escapar una
    excepción: si lo hiciera, APScheduler la registraría pero el job quedaría
    igualmente sin ejecutar hasta el siguiente intervalo, sin rastro claro."""
    try:
        async with AsyncSessionLocal() as db:
            usuario_sistema_id = await id_usuario_sistema(db)
            await procesar_alertas_pendientes(db, usuario_sistema_id=usuario_sistema_id)
    except Exception:
        logger.exception("Motor de alertas: el ciclo no pudo completarse.")


def iniciar_scheduler() -> AsyncIOScheduler | None:
    """Arranca el scheduler si está habilitado; si no, devuelve None.

    Desactivado por defecto: en tests y en la Lambda del Hito 7 no debe
    arrancar nada por su cuenta.
    """
    if not settings.scheduler_habilitado:
        logger.info("Scheduler desactivado (SCHEDULER_HABILITADO=false): el motor de alertas no se dispara.")
        return None

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        ciclo_motor_alertas,
        "interval",
        minutes=settings.scheduler_intervalo_minutos,
        id=JOB_MOTOR_ALERTAS,
        # Un ciclo a la vez: si uno se alarga más que el intervalo, el
        # siguiente no se solapa, y los disparos perdidos se juntan en uno.
        max_instances=1,
        coalesce=True,
        # Primer ciclo al arrancar, sin esperar un intervalo entero: al
        # desplegar, lo que estuviera vencido se atiende ya. No duplica
        # trabajo, porque quien decide es ultima_ejecucion_at.
        next_run_time=datetime.now(UTC),
    )
    scheduler.start()
    logger.info(
        "Scheduler arrancado: motor de alertas cada %d minuto(s).", settings.scheduler_intervalo_minutos
    )
    return scheduler


async def parar_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    """Para el scheduler, si hay alguno arrancado.

    `wait=False` para que un ciclo en curso no bloquee el apagado de la app:
    lo que quede a medias se retoma en el arranque siguiente, porque
    `ultima_ejecucion_at` solo avanza con la alerta ya procesada.

    Es `async` por una peculiaridad de APScheduler 3.x: su `shutdown` va
    decorado con `run_in_event_loop`, así que no apaga en el momento, sino en
    la siguiente vuelta del event loop. Cediendo el control aquí, la función
    devuelve con el scheduler ya parado de verdad y el apagado de la app no
    se queda a medias.
    """
    if scheduler is None or not scheduler.running:
        return
    scheduler.shutdown(wait=False)
    await asyncio.sleep(0)
    logger.info("Scheduler detenido.")
