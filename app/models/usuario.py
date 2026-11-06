from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
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
        UniqueConstraint("calendario_token", name="uq_usuario_calendario_token"),
        CheckConstraint(r"telefono IS NULL OR telefono ~ '^\+?[0-9 ()\-.]{6,30}$'", name="ck_usuario_telefono"),
        CheckConstraint("cargo IS NULL OR length(trim(cargo)) > 0", name="ck_usuario_cargo"),
        CheckConstraint("avatar_sha256 IS NULL OR avatar_sha256 ~ '^[0-9a-f]{64}$'", name="ck_usuario_avatar_sha256"),
        CheckConstraint(
            "calendario_token IS NULL OR length(calendario_token) >= 32", name="ck_usuario_calendario_token"
        ),
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
    # Opcional (plan N11). El formato lo valida el schema y lo repite el CHECK de la tabla.
    telefono: Mapped[str | None] = mapped_column(String(30))
    # Secret of the personal calendar feed (plan N6). `secrets.token_urlsafe(32)`, created on first use and
    # replaced on rotation. Never serialised by the user schemas.
    calendario_token: Mapped[str | None] = mapped_column(String(64))
    cargo: Mapped[str | None] = mapped_column(String(100))
    # sha256 of the stored WebP (content-addressed in the document store); NULL = no avatar.
    avatar_sha256: Mapped[str | None] = mapped_column(String(64))

    @property
    def avatar_url(self) -> str | None:
        """Versioned URL (the version is a hash prefix, so a new image busts the cache)."""
        if self.avatar_sha256 is None:
            return None
        return f"/usuarios/{self.id}/avatar?v={self.avatar_sha256[:16]}"
