from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class AlertaEjecucionConvocatoria(Base, AuditMixin):
    """Qué convocatorias arrojó cada ejecución de alerta — la unicidad es lo
    que impide notificar dos veces la misma convocatoria en una misma alerta."""

    __tablename__ = "alerta_ejecucion_convocatoria"
    __table_args__ = (
        UniqueConstraint("alerta_ejecucion_id", "convocatoria_id", name="uq_alerta_ejecucion_convocatoria"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alerta_ejecucion_id: Mapped[int] = mapped_column(
        ForeignKey("alerta_ejecucion.id", ondelete="CASCADE"), nullable=False
    )
    convocatoria_id: Mapped[int] = mapped_column(ForeignKey("convocatoria.id"), nullable=False)
