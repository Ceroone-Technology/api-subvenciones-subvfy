"""Salud de la sincronización con la BDNS (solo admin)."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api.deps import AdminDep, DbDep
from app.models import SyncRun

router = APIRouter(prefix="/admin/sync", tags=["admin-sync"])

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
    ultimo_ok = await db.scalar(select(func.max(SyncRun.finished_at)).where(SyncRun.estado == "ok"))
    ejecuciones = (await db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(ULTIMAS_EJECUCIONES))).all()
    return EstadoSync(
        sano=ultimo_ok is not None and ultimo_ok > ahora() - UMBRAL_SANO,
        ultimo_ok_at=ultimo_ok,
        ejecuciones=[EjecucionSync.model_validate(e) for e in ejecuciones],
    )
