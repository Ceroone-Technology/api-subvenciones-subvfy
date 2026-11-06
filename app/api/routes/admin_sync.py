"""Salud de la sincronización con la BDNS (solo admin)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, or_, select

from app.api.deps import AdminDep, DbDep
from app.config import settings
from app.models import SyncRun
from app.services import sync_resumen as servicio_resumen
from app.services.sync_bdns import MOTIVO_PRESUPUESTO, ejecutar_sync
from app.services.sync_resumen import MAX_DIAS_SERIE, PuntoSerie, TotalesSync

router = APIRouter(prefix="/admin/sync", tags=["admin-sync"])
logger = logging.getLogger(__name__)

# Sin un run `ok` en este margen la sincronización no está sana (3 disparos al día).
UMBRAL_SANO = timedelta(hours=12)
ULTIMAS_EJECUCIONES = 10


# Reloj sustituible en los tests.
def _ahora() -> datetime:
    return datetime.now(UTC)


ahora: Callable[[], datetime] = _ahora


class EjecucionSync(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    modo: str
    estado: str
    started_at: datetime
    finished_at: datetime | None
    ventana_desde: date | None
    ventana_hasta: date | None
    listadas: int
    detalles: int
    insertadas: int
    actualizadas: int
    retiradas: int
    errores: int
    organos_sin_resolver: int
    peticiones_bdns: int
    error_detalle: str | None
    cursor: dict[str, Any] | None


class EstadoSync(BaseModel):
    sano: bool
    ultimo_ok_at: datetime | None
    ejecuciones: list[EjecucionSync]


@router.get("/estado", response_model=EstadoSync, summary="Estado de la sincronización con la BDNS")
async def estado_sync(db: DbDep, actual: AdminDep) -> EstadoSync:
    # Sano = un run `ok`, o `partial` solo porque se agotó el presupuesto de refresco (el siguiente continúa).
    # Un `partial` por caja de tiempo o por retiradas masivas abortadas no cuenta.
    sano = or_(
        SyncRun.estado == "ok",
        and_(
            SyncRun.estado == "partial",
            SyncRun.error_detalle.startswith(MOTIVO_PRESUPUESTO),
            SyncRun.errores * 2 < SyncRun.detalles,  # con la mitad de detalles fallidos no hay señal de salud
        ),
    )
    ultimo_ok = await db.scalar(select(func.max(SyncRun.finished_at)).where(sano))
    ejecuciones = (await db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(ULTIMAS_EJECUCIONES))).all()
    return EstadoSync(
        sano=ultimo_ok is not None and ultimo_ok > ahora() - UMBRAL_SANO,
        ultimo_ok_at=ultimo_ok,
        ejecuciones=[EjecucionSync.model_validate(e) for e in ejecuciones],
    )


# --- Forced run, daily series and totals (plan N15) --------------------------------------------------------


class RunAceptado(BaseModel):
    estado: str
    mensaje: str


# The task of a forced run started by this process; a second click while it runs is a 409 and not a second task.
# Across processes the advisory lock of `ejecutar` is the guarantee. A run started here lives with the process
# (fine for the local server; in Lambda the forced run will be an invocation of the sync function instead).
_tarea_forzada: asyncio.Task[None] | None = None


async def _correr_sync_forzado(admin_id: int) -> None:
    try:
        resultado = await ejecutar_sync()
    except Exception:
        logger.exception("Sync BDNS forzado por el admin %s: el run no pudo completarse.", admin_id)
        return
    if resultado is None:
        logger.info("Sync BDNS forzado por el admin %s: había otro run con el lock.", admin_id)
    else:
        logger.info("Sync forzado por el admin %s: run %s, estado %s.", admin_id, resultado.run_id, resultado.estado)


@router.post(
    "/forzar",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunAceptado,
    summary="Forzar una sincronización incremental ahora",
    responses={409: {"description": "Ya hay una sincronización en curso."}},
)
async def forzar_sync(db: DbDep, actual: AdminDep) -> RunAceptado:
    """Lanza el run en segundo plano y responde 202 al momento; el avance se ve en `GET /admin/sync/estado`."""
    global _tarea_forzada
    en_curso = await db.scalar(
        select(SyncRun.id)
        .where(
            SyncRun.estado == "running",
            SyncRun.started_at > ahora() - timedelta(minutes=2 * settings.sync_max_minutos),
        )
        .limit(1)
    )
    if (_tarea_forzada is not None and not _tarea_forzada.done()) or en_curso is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Ya hay una sincronización en curso.")
    _tarea_forzada = asyncio.create_task(_correr_sync_forzado(actual.id))
    return RunAceptado(estado="aceptada", mensaje="Sincronización lanzada. Su avance se ve en /admin/sync/estado.")


@router.get("/serie", response_model=list[PuntoSerie], summary="Ejecuciones de la sincronización por día")
async def serie_sync(
    db: DbDep, actual: AdminDep, dias: Annotated[int, Query(ge=1, le=MAX_DIAS_SERIE)] = 14
) -> list[PuntoSerie]:
    """Un punto por día, del más antiguo al de hoy, con los días sin ejecuciones a cero."""
    return await servicio_resumen.serie(db, dias)


@router.get("/totales", response_model=TotalesSync, summary="Totales de la copia local de la BDNS")
async def totales_sync(db: DbDep, actual: AdminDep) -> TotalesSync:
    return await servicio_resumen.totales(db, ahora=ahora())
