from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

ROLES_EMISOR = ("usuario", "asistente")


class MensajeAsistente(Base, AuditMixin):
    __tablename__ = "mensaje_asistente"
    __table_args__ = (
        CheckConstraint(f"rol_emisor IN {ROLES_EMISOR}", name="ck_mensaje_rol_emisor"),
        Index("ix_mensaje_conversacion", "conversacion_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversacion_id: Mapped[int] = mapped_column(
        ForeignKey("conversacion_asistente.id", ondelete="CASCADE"), nullable=False
    )
    convocatoria_id: Mapped[int | None] = mapped_column(ForeignKey("convocatoria.id"))
    rol_emisor: Mapped[str] = mapped_column(String(20), nullable=False)
    contenido: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer)
