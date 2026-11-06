from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

# bdns: served right after downloading it from BDNS. cache: served from our storage.
ORIGENES_ACCESO = ("bdns", "cache")


class DocumentoAcceso(Base, AuditMixin):
    """One successful document serve. Only ids: no email, IP or content (D16)."""

    __tablename__ = "documento_acceso"
    __table_args__ = (
        CheckConstraint(f"origen IN {ORIGENES_ACCESO}", name="ck_documento_acceso_origen"),
        Index("ix_documento_acceso_documento", "documento_id", text("accedido_at DESC")),
        Index("ix_documento_acceso_usuario", "usuario_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    documento_id: Mapped[int] = mapped_column(ForeignKey("convocatoria_documento.id"), nullable=False)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), nullable=False)
    accedido_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    origen: Mapped[str] = mapped_column(String(10), nullable=False)
