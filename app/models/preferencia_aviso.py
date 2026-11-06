from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

# Kinds of notice a user can switch per channel (plan N9a), in display order. The CHECK of the table is written by
# hand in the migration; change both together.
TIPOS_AVISO = ("nuevas", "plazos", "alertas", "semanal", "seguimiento")

# Defaults of the design for a user with no stored row: (email, app).
PREFERENCIAS_POR_DEFECTO: dict[str, tuple[bool, bool]] = {
    "nuevas": (True, True),
    "plazos": (True, True),
    "alertas": (True, False),
    "semanal": (True, False),
    "seguimiento": (False, True),
}


class PreferenciaAviso(Base, AuditMixin):
    """Per-user, per-kind channel switches. Only rows the user saved exist; the rest fall back to the defaults."""

    __tablename__ = "usuario_preferencia_aviso"
    __table_args__ = (CheckConstraint(f"tipo IN {TIPOS_AVISO}", name="ck_usuario_preferencia_aviso_tipo"),)

    usuario_id: Mapped[int] = mapped_column(
        ForeignKey("usuario.id", ondelete="CASCADE", name="fk_usuario_preferencia_aviso_usuario"), primary_key=True
    )
    tipo: Mapped[str] = mapped_column(String(20), primary_key=True)
    email: Mapped[bool] = mapped_column(Boolean, nullable=False)
    app: Mapped[bool] = mapped_column(Boolean, nullable=False)
