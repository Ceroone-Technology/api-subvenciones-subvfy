from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

TAMANOS_VALIDOS = ("micro", "pequena", "mediana", "grande")
ESTADOS_EMPRESA = ("activa", "inactiva")


class Empresa(Base, AuditMixin):
    """Tenant: cada usuario pertenece a una empresa (B2B, multi-tenant)."""

    __tablename__ = "empresa"
    __table_args__ = (
        CheckConstraint(f"tamano IN {TAMANOS_VALIDOS}", name="ck_empresa_tamano"),
        CheckConstraint(f"estado IN {ESTADOS_EMPRESA}", name="ck_empresa_estado"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    razon_social: Mapped[str] = mapped_column(String(200), nullable=False)
    nif: Mapped[str] = mapped_column(String(15), unique=True, nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100))
    tamano: Mapped[str | None] = mapped_column(String(20))
    ccaa: Mapped[str | None] = mapped_column(String(100))
    descripcion: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="activa", server_default="activa")
