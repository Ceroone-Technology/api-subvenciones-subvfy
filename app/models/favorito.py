from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class Favorito(Base, AuditMixin):
    __tablename__ = "favorito"
    __table_args__ = (
        UniqueConstraint("usuario_id", "convocatoria_id", name="uq_favorito_usuario_convocatoria"),
        Index("ix_favorito_usuario", "usuario_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False)
    convocatoria_id: Mapped[int] = mapped_column(ForeignKey("convocatoria.id", ondelete="CASCADE"), nullable=False)
    nota: Mapped[str | None] = mapped_column(Text)
