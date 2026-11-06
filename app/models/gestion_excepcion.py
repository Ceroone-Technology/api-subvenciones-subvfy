from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class GestionExcepcion(Base, AuditMixin):
    """Admin override of "Subvfy manages this grant" for one convocatoria (N1).

    The rule says digitalización and I+D are managed; a row here wins over it in either direction
    (`gestiona = true` for a grant of another theme, `false` for a dig/id one). Deleting the row
    returns the grant to the rule.
    """

    __tablename__ = "gestion_excepcion"
    __table_args__ = (UniqueConstraint("convocatoria_id", name="uq_gestion_excepcion_convocatoria"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    convocatoria_id: Mapped[int] = mapped_column(
        ForeignKey("convocatoria.id", name="fk_gestion_excepcion_convocatoria"), nullable=False
    )
    gestiona: Mapped[bool] = mapped_column(Boolean, nullable=False)
    motivo: Mapped[str | None] = mapped_column(String(300))
