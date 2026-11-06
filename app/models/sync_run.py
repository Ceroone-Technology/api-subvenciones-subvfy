from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

MODOS_SYNC = ("incremental", "backfill")
# running: in progress. ok: finished. partial: stopped by the time box or the
# refresh budget (the next run continues). failed: the circuit breaker opened.
ESTADOS_SYNC = ("running", "ok", "partial", "failed")


def _contador() -> Mapped[int]:
    return mapped_column(Integer, nullable=False, default=0, server_default="0")


class SyncRun(Base, AuditMixin):
    """One execution of the BDNS sync: counts, window and checkpoint.

    A single table covers health (`GET /admin/sync/estado`), resume (the
    `cursor`) and metrics. The incremental watermark is the greatest
    `ventana_hasta` among `ok` incremental runs.
    """

    __tablename__ = "sync_run"
    __table_args__ = (
        CheckConstraint(f"modo IN {MODOS_SYNC}", name="ck_sync_run_modo"),
        CheckConstraint(f"estado IN {ESTADOS_SYNC}", name="ck_sync_run_estado"),
        Index("ix_sync_run_modo_estado", "modo", "estado", text("finished_at DESC")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    modo: Mapped[str] = mapped_column(String(15), nullable=False)
    estado: Mapped[str] = mapped_column(String(10), nullable=False, default="running", server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ventana_desde: Mapped[date | None] = mapped_column(Date)
    ventana_hasta: Mapped[date | None] = mapped_column(Date)
    listadas: Mapped[int] = _contador()
    detalles: Mapped[int] = _contador()
    insertadas: Mapped[int] = _contador()
    actualizadas: Mapped[int] = _contador()
    retiradas: Mapped[int] = _contador()
    errores: Mapped[int] = _contador()
    organos_sin_resolver: Mapped[int] = _contador()
    peticiones_bdns: Mapped[int] = _contador()
    error_detalle: Mapped[str | None] = mapped_column(String(500))
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
