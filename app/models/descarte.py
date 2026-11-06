from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class Descarte(Base, AuditMixin):
    """A grant the user hid ("No me interesa"). Personal, like favorites: it never affects colleagues.

    The radar and the news feeds leave these out; the search does not (a hidden grant can still be looked up).
    """

    __tablename__ = "descarte"
    __table_args__ = (
        UniqueConstraint("usuario_id", "convocatoria_id", name="uq_descarte_usuario_convocatoria"),
        Index("ix_descarte_usuario", "usuario_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False)
    convocatoria_id: Mapped[int] = mapped_column(ForeignKey("convocatoria.id", ondelete="CASCADE"), nullable=False)
