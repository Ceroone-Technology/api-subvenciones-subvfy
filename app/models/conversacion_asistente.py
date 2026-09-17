from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

ESTADOS_CONVERSACION = ("activa", "archivada")


class ConversacionAsistente(Base, AuditMixin):
    __tablename__ = "conversacion_asistente"
    __table_args__ = (
        CheckConstraint(f"estado IN {ESTADOS_CONVERSACION}", name="ck_conversacion_estado"),
        Index("ix_conversacion_usuario", "usuario_id", "ultima_actividad_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False)
    titulo: Mapped[str] = mapped_column(
        String(200), nullable=False, default="Nueva conversación", server_default="Nueva conversación"
    )
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="activa", server_default="activa")
    ultima_actividad_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
