from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

NIVELES_ADMINISTRACION = ("estado", "ccaa", "local", "otros")
FRECUENCIAS_VALIDAS = ("inmediata", "diaria", "semanal")
CANALES_NOTIFICACION = ("email", "plataforma", "ambos")


class Alerta(Base, AuditMixin):
    __tablename__ = "alerta"
    __table_args__ = (
        CheckConstraint(f"nivel_administracion IN {NIVELES_ADMINISTRACION}", name="ck_alerta_nivel"),
        CheckConstraint(f"frecuencia IN {FRECUENCIAS_VALIDAS}", name="ck_alerta_frecuencia"),
        CheckConstraint(f"canal_notificacion IN {CANALES_NOTIFICACION}", name="ck_alerta_canal"),
        CheckConstraint(
            "fecha_desde IS NULL OR fecha_hasta IS NULL OR fecha_desde <= fecha_hasta",
            name="ck_alerta_rango_fechas",
        ),
        Index("ix_alerta_usuario", "usuario_id"),
        Index("ix_alerta_activa", "activa", postgresql_where="activa = true"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(150), nullable=False)
    texto_busqueda: Mapped[str | None] = mapped_column(String(300))
    nivel_administracion: Mapped[str | None] = mapped_column(String(20))
    fecha_desde: Mapped[date | None] = mapped_column(Date)
    fecha_hasta: Mapped[date | None] = mapped_column(Date)
    solo_mrr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    frecuencia: Mapped[str] = mapped_column(String(20), nullable=False, default="diaria", server_default="diaria")
    canal_notificacion: Mapped[str] = mapped_column(
        String(20), nullable=False, default="email", server_default="email"
    )
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    ultima_ejecucion_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
