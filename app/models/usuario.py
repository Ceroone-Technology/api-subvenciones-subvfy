from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

ESTADOS_USUARIO = ("activo", "inactivo", "bloqueado")


class Usuario(Base, AuditMixin):
    __tablename__ = "usuario"
    __table_args__ = (
        CheckConstraint(f"estado IN {ESTADOS_USUARIO}", name="ck_usuario_estado"),
        Index("ix_usuario_empresa", "empresa_id"),
        Index("ix_usuario_email", "email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    rol_id: Mapped[int] = mapped_column(ForeignKey("rol.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    apellidos: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="activo", server_default="activo")
    ultimo_acceso_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
