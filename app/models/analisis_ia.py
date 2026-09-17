from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

TIPOS_ANALISIS = ("resumen", "requisitos_clave", "idoneidad", "riesgos")
ESTADOS_ANALISIS = ("pendiente", "procesando", "completado", "error")


class AnalisisIA(Base, AuditMixin):
    """Resultado de un análisis con IA sobre una convocatoria. `resultado`
    es jsonb: la forma exacta depende de `tipo_analisis` y del prompt usado
    (Hito 5), así que no se fuerza un schema rígido en columnas separadas."""

    __tablename__ = "analisis_ia"
    __table_args__ = (
        CheckConstraint(f"tipo_analisis IN {TIPOS_ANALISIS}", name="ck_analisis_tipo"),
        CheckConstraint(f"estado IN {ESTADOS_ANALISIS}", name="ck_analisis_estado"),
        CheckConstraint(
            "score_idoneidad IS NULL OR tipo_analisis = 'idoneidad'",
            name="ck_analisis_score_solo_idoneidad",
        ),
        Index("ix_analisis_ia_convocatoria", "convocatoria_id"),
        Index("ix_analisis_ia_empresa", "empresa_id", postgresql_where="empresa_id IS NOT NULL"),
        Index("ix_analisis_ia_resultado", "resultado", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    convocatoria_id: Mapped[int] = mapped_column(ForeignKey("convocatoria.id", ondelete="CASCADE"), nullable=False)
    empresa_id: Mapped[int | None] = mapped_column(ForeignKey("empresa.id"))  # NULL = análisis genérico
    tipo_analisis: Mapped[str] = mapped_column(String(30), nullable=False)
    modelo_ia: Mapped[str] = mapped_column(String(60), nullable=False)
    resultado: Mapped[dict] = mapped_column(JSONB, nullable=False)
    score_idoneidad: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="pendiente", server_default="pendiente")
    tokens_entrada: Mapped[int | None] = mapped_column(Integer)
    tokens_salida: Mapped[int | None] = mapped_column(Integer)
    coste_estimado: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    generado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
