from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class AlertaRegion(Base, AuditMixin):
    """Regiones (CCAA) filtradas por una alerta (multivalor normalizado;
    region_bdns_id referencia el catálogo externo de regiones de la BDNS)."""

    __tablename__ = "alerta_region"
    __table_args__ = (
        UniqueConstraint("alerta_id", "region_bdns_id", name="uq_alerta_region"),
        # Búsqueda inversa del motor de alertas: qué alertas vigilan una región.
        Index("ix_alerta_region_region", "region_bdns_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alerta_id: Mapped[int] = mapped_column(ForeignKey("alerta.id", ondelete="CASCADE"), nullable=False)
    region_bdns_id: Mapped[int] = mapped_column(Integer, nullable=False)
