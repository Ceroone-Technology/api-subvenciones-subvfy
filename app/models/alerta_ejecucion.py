from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

ESTADOS_ENVIO = ("enviado", "sin_novedades", "error")


class AlertaEjecucion(Base, AuditMixin):
    """Historial de ejecuciones del motor de alertas — evita re-notificar la
    misma convocatoria (ver alerta_ejecucion_convocatoria)."""

    __tablename__ = "alerta_ejecucion"
    __table_args__ = (
        CheckConstraint(f"estado_envio IN {ESTADOS_ENVIO}", name="ck_alerta_ejecucion_estado"),
        Index("ix_alerta_ejecucion_alerta", "alerta_id", "fecha_ejecucion_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alerta_id: Mapped[int] = mapped_column(ForeignKey("alerta.id", ondelete="CASCADE"), nullable=False)
    fecha_ejecucion_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    convocatorias_encontradas: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    estado_envio: Mapped[str] = mapped_column(String(20), nullable=False)
    detalle_error: Mapped[str | None] = mapped_column(Text)
