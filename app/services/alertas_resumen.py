"""Summary and history figures of the alerts list (plan N8). Everything here is read-only.

`historial` is the last `EJECUCIONES_POR_ALERTA` runs of an alert, **newest first** (the same order as
`GET /alertas/{id}/ejecuciones`; the frontend reverses it to draw the bars oldest to newest).
"""

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Alerta, AlertaEjecucion
from app.schemas.alerta import AlertasResumen
from app.schemas.alerta_ejecucion import EjecucionRead
from app.services.motor_alertas import INTERVALOS

EJECUCIONES_POR_ALERTA = 8
DIAS_NOVEDADES = 7


def _siguiente_ciclo(instante: datetime) -> datetime:
    """First engine cycle at or after `instante`, on the grid of `SCHEDULER_INTERVALO_MINUTOS` (UTC, from epoch)."""
    paso = settings.scheduler_intervalo_minutos * 60
    segundos = instante.timestamp()
    return datetime.fromtimestamp(math.ceil(segundos / paso) * paso, tz=UTC)


def calcular_proxima_ejecucion(alerta: Alerta, ahora: datetime) -> datetime | None:
    """When the engine will take the alert up, or None when it is paused.

    Same rule as `alertas_pendientes`: never run = due now; otherwise last run plus the interval of its
    frequency; and never before the wait that follows a failure. The engine only acts at its cycles, so the
    answer is the first cycle on the grid of `SCHEDULER_INTERVALO_MINUTOS` that is at or after the due moment
    and strictly after `ahora`: always in the future, and stable within a cycle (the frontend ignores past values).
    """
    if not alerta.activa:
        return None
    pendiente = ahora
    if alerta.ultima_ejecucion_at is not None:
        pendiente = alerta.ultima_ejecucion_at + INTERVALOS[alerta.frecuencia]
    if alerta.proximo_reintento_at is not None and alerta.proximo_reintento_at > pendiente:
        pendiente = alerta.proximo_reintento_at
    ciclo = _siguiente_ciclo(max(pendiente, ahora))
    return ciclo if ciclo > ahora else _siguiente_ciclo(ahora + timedelta(seconds=1))


async def historiales(db: AsyncSession, alerta_ids: Sequence[int]) -> dict[int, list[EjecucionRead]]:
    """Last runs of several alerts in one query (window function), newest first."""
    resultado: dict[int, list[EjecucionRead]] = defaultdict(list)
    if not alerta_ids:
        return resultado
    orden = func.row_number().over(
        partition_by=AlertaEjecucion.alerta_id,
        order_by=(AlertaEjecucion.fecha_ejecucion_at.desc(), AlertaEjecucion.id.desc()),
    )
    numeradas = (
        select(AlertaEjecucion.id.label("id"), orden.label("n"))
        .where(AlertaEjecucion.alerta_id.in_(alerta_ids))
        .subquery()
    )
    filas = await db.scalars(
        select(AlertaEjecucion)
        .join(numeradas, numeradas.c.id == AlertaEjecucion.id)
        .where(numeradas.c.n <= EJECUCIONES_POR_ALERTA)
        .order_by(AlertaEjecucion.alerta_id, AlertaEjecucion.fecha_ejecucion_at.desc(), AlertaEjecucion.id.desc())
        .execution_options(populate_existing=True)
    )
    for ejecucion in filas:
        resultado[ejecucion.alerta_id].append(EjecucionRead.model_validate(ejecucion))
    return resultado


async def resumen(db: AsyncSession, usuario_id: int, *, ahora: datetime | None = None) -> AlertasResumen:
    ahora = ahora or datetime.now(UTC)
    alertas = (await db.scalars(select(Alerta).where(Alerta.usuario_id == usuario_id))).all()
    ids = [a.id for a in alertas]
    activas = [a for a in alertas if a.activa]

    novedades = 0
    ultimas: dict[int, str] = {}
    if ids:
        novedades = (
            await db.scalar(
                select(func.coalesce(func.sum(AlertaEjecucion.convocatorias_encontradas), 0)).where(
                    AlertaEjecucion.alerta_id.in_(ids),
                    AlertaEjecucion.fecha_ejecucion_at >= ahora - timedelta(days=DIAS_NOVEDADES),
                )
            )
            or 0
        )
        ultima_por_alerta = await db.execute(
            select(AlertaEjecucion.alerta_id, AlertaEjecucion.estado_envio)
            .where(AlertaEjecucion.alerta_id.in_(ids))
            .distinct(AlertaEjecucion.alerta_id)
            .order_by(AlertaEjecucion.alerta_id, AlertaEjecucion.fecha_ejecucion_at.desc(), AlertaEjecucion.id.desc())
        )
        ultimas = {alerta_id: estado for alerta_id, estado in ultima_por_alerta}

    proximas = [p for a in activas if (p := calcular_proxima_ejecucion(a, ahora)) is not None]
    return AlertasResumen(
        activas=len(activas),
        total=len(alertas),
        novedades_semana=int(novedades),
        requieren_atencion=sum(1 for a in activas if ultimas.get(a.id) == "error"),
        proxima_ejecucion_at=min(proximas) if proximas else None,
    )
