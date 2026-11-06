"""Read-only figures of the BDNS sync for the admin screen (plan N15): daily series and totals."""

from datetime import date, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Convocatoria, SyncRun
from app.services.convocatoria_estado import ABIERTA, ABIERTA_SIN_PLAZO, estado_expr, hoy_madrid

DIAS_FALLOS = 14
MAX_DIAS_SERIE = 90


class PuntoSerie(BaseModel):
    """One day (Europe/Madrid, by the start of the run). Days without runs are present with zeros."""

    fecha: date
    ejecuciones: int
    # Runs that ended `failed` (circuit breaker open, lost lock, crash).
    fallidas: int
    insertadas: int
    actualizadas: int
    retiradas: int
    # Rows that could not be processed inside the runs (not the same as failed runs).
    errores: int


class TotalesSync(BaseModel):
    en_base_de_datos: int
    # `Abierta` and `Abierta sin plazo`, computed against today in Madrid.
    abiertas: int
    fallos_14_dias: int
    ultima_sincronizacion_at: datetime | None


async def serie(db: AsyncSession, dias: int, *, hoy: date | None = None) -> list[PuntoSerie]:
    dia = cast(func.timezone(settings.sync_zona, SyncRun.started_at), Date)
    hoy = hoy or await db.scalar(select(hoy_madrid())) or date.today()
    desde = hoy - timedelta(days=dias - 1)
    filas = await db.execute(
        select(
            dia,
            func.count(),
            func.count().filter(SyncRun.estado == "failed"),
            func.coalesce(func.sum(SyncRun.insertadas), 0),
            func.coalesce(func.sum(SyncRun.actualizadas), 0),
            func.coalesce(func.sum(SyncRun.retiradas), 0),
            func.coalesce(func.sum(SyncRun.errores), 0),
        )
        .where(dia >= desde, dia <= hoy)
        .group_by(dia)
    )
    por_dia = {f[0]: f[1:] for f in filas}
    vacio = (0, 0, 0, 0, 0, 0)
    puntos = []
    for i in range(dias):
        d = desde + timedelta(days=i)
        n, fallidas, ins, act, ret, err = por_dia.get(d, vacio)
        puntos.append(
            PuntoSerie(
                fecha=d, ejecuciones=n, fallidas=fallidas, insertadas=int(ins), actualizadas=int(act),
                retiradas=int(ret), errores=int(err),
            )  # fmt: skip
        )
    return puntos


async def totales(db: AsyncSession, *, ahora: datetime) -> TotalesSync:
    estado = estado_expr()
    en_bd, abiertas = (
        await db.execute(
            select(func.count(), func.count().filter(estado.in_((ABIERTA, ABIERTA_SIN_PLAZO)))).where(
                Convocatoria.removed_at.is_(None)
            )
        )
    ).one()
    fallos = await db.scalar(
        select(func.count()).where(
            SyncRun.estado == "failed", SyncRun.started_at >= ahora - timedelta(days=DIAS_FALLOS)
        )
    )
    ultima = await db.scalar(select(func.max(SyncRun.finished_at)).where(SyncRun.estado.in_(("ok", "partial"))))
    return TotalesSync(
        en_base_de_datos=en_bd, abiertas=abiertas, fallos_14_dias=fallos or 0, ultima_sincronizacion_at=ultima
    )
