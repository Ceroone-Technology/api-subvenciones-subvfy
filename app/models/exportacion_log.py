from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

FORMATOS_EXPORTACION = ("csv", "xlsx")


class ExportacionLog(Base, AuditMixin):
    """One export of the grants list (who, from which company, format, filters, rows). Feeds the daily limit."""

    __tablename__ = "exportacion_log"
    __table_args__ = (
        CheckConstraint(f"formato IN {FORMATOS_EXPORTACION}", name="ck_exportacion_log_formato"),
        CheckConstraint("filas >= 0", name="ck_exportacion_log_filas"),
        Index("ix_exportacion_log_usuario_fecha", "usuario_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id", ondelete="CASCADE"), nullable=False)
    formato: Mapped[str] = mapped_column(String(4), nullable=False)
    filtros: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    filas: Mapped[int] = mapped_column(Integer, nullable=False)
