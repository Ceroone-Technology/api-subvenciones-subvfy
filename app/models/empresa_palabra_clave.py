from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class EmpresaPalabraClave(Base, AuditMixin):
    """Palabras clave del perfil de una empresa (usadas por el Análisis con IA
    para calificar la idoneidad de una convocatoria)."""

    __tablename__ = "empresa_palabra_clave"
    __table_args__ = (UniqueConstraint("empresa_id", "palabra", name="uq_empresa_palabra"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id", ondelete="CASCADE"), nullable=False)
    palabra: Mapped[str] = mapped_column(String(80), nullable=False)
