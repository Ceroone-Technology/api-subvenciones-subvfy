from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class AlertaOrgano(Base, AuditMixin):
    """Órganos convocantes filtrados por una alerta (multivalor normalizado;
    organo_bdns_id referencia el catálogo jerárquico externo de la BDNS,
    no una tabla propia)."""

    __tablename__ = "alerta_organo"
    __table_args__ = (UniqueConstraint("alerta_id", "organo_bdns_id", name="uq_alerta_organo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    alerta_id: Mapped[int] = mapped_column(ForeignKey("alerta.id", ondelete="CASCADE"), nullable=False)
    organo_bdns_id: Mapped[int] = mapped_column(Integer, nullable=False)
