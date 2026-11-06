from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

# The four columns of the board, in order. `resuelta` is the only state that can carry a `resultado`.
ESTADOS_SEGUIMIENTO = ("interesa", "preparando", "presentada", "resuelta")
RESULTADOS_SEGUIMIENTO = ("concedida", "denegada")
TIPOS_TAREA = ("documento", "tarea")
TIPOS_EVENTO = ("creada", "movida", "asignada", "resuelta")


class Seguimiento(Base, AuditMixin):
    """A grant on the board of a company (plan N5a). The unit is the company, not the user."""

    __tablename__ = "seguimiento"
    __table_args__ = (
        UniqueConstraint("empresa_id", "convocatoria_id", name="uq_seguimiento_empresa_convocatoria"),
        CheckConstraint(f"estado IN {ESTADOS_SEGUIMIENTO}", name="ck_seguimiento_estado"),
        CheckConstraint(f"resultado IN {RESULTADOS_SEGUIMIENTO}", name="ck_seguimiento_resultado"),
        CheckConstraint("resultado IS NULL OR estado = 'resuelta'", name="ck_seguimiento_resultado_resuelta"),
        Index("ix_seguimiento_empresa_estado", "empresa_id", "estado"),
        Index("ix_seguimiento_responsable", "responsable_usuario_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id", ondelete="CASCADE"), nullable=False)
    convocatoria_id: Mapped[int] = mapped_column(ForeignKey("convocatoria.id", ondelete="CASCADE"), nullable=False)
    estado: Mapped[str] = mapped_column(String(12), nullable=False, default="interesa", server_default="interesa")
    resultado: Mapped[str | None] = mapped_column(String(10))
    responsable_usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id", ondelete="SET NULL"))
    fecha_presentacion: Mapped[date | None] = mapped_column(Date)


class SeguimientoTarea(Base, AuditMixin):
    """Checklist item of a card. `documento` ones feed the "Documentación x/y" bar (plan N5b)."""

    __tablename__ = "seguimiento_tarea"
    __table_args__ = (
        CheckConstraint(f"tipo IN {TIPOS_TAREA}", name="ck_seguimiento_tarea_tipo"),
        CheckConstraint("length(btrim(titulo)) > 0", name="ck_seguimiento_tarea_titulo"),
        Index("ix_seguimiento_tarea_seguimiento", "seguimiento_id"),
        Index("ix_seguimiento_tarea_fecha", "fecha_limite", postgresql_where="fecha_limite IS NOT NULL"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seguimiento_id: Mapped[int] = mapped_column(ForeignKey("seguimiento.id", ondelete="CASCADE"), nullable=False)
    titulo: Mapped[str] = mapped_column(String(200), nullable=False)
    tipo: Mapped[str] = mapped_column(String(10), nullable=False, default="tarea", server_default="tarea")
    fecha_limite: Mapped[date | None] = mapped_column(Date)
    hecha: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    asignado_a: Mapped[int | None] = mapped_column(ForeignKey("usuario.id", ondelete="SET NULL"))


class SeguimientoEvento(Base, AuditMixin):
    """Append-only log of what happened to a card (who moved it, to where, when)."""

    __tablename__ = "seguimiento_evento"
    __table_args__ = (
        CheckConstraint(f"tipo IN {TIPOS_EVENTO}", name="ck_seguimiento_evento_tipo"),
        Index("ix_seguimiento_evento_seguimiento", "seguimiento_id", "ocurrido_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seguimiento_id: Mapped[int] = mapped_column(ForeignKey("seguimiento.id", ondelete="CASCADE"), nullable=False)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id", ondelete="SET NULL"))
    tipo: Mapped[str] = mapped_column(String(10), nullable=False)
    estado_anterior: Mapped[str | None] = mapped_column(String(12))
    estado_nuevo: Mapped[str | None] = mapped_column(String(12))
    ocurrido_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
