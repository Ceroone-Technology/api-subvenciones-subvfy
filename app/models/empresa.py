from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin
from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA

TAMANOS_VALIDOS = ("micro", "pequena", "mediana", "grande")
ESTADOS_EMPRESA = ("activa", "inactiva")


class Empresa(Base, AuditMixin):
    """Tenant: cada usuario pertenece a una empresa (B2B, multi-tenant)."""

    __tablename__ = "empresa"
    __table_args__ = (
        CheckConstraint(f"tamano IN {TAMANOS_VALIDOS}", name="ck_empresa_tamano"),
        CheckConstraint(f"estado IN {ESTADOS_EMPRESA}", name="ck_empresa_estado"),
        # Written by hand in the migration too: Alembic does not compare CHECKs.
        CheckConstraint(
            f"actividad_codigo <> '{CODIGO_ACTIVIDAD_OTRA}' OR length(trim(actividad_otra)) > 0",
            name="ck_empresa_actividad_otra",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    razon_social: Mapped[str] = mapped_column(String(200), nullable=False)
    nif: Mapped[str] = mapped_column(String(15), unique=True, nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100))
    tamano: Mapped[str | None] = mapped_column(String(20))
    ccaa: Mapped[str | None] = mapped_column(String(100))
    descripcion: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="activa", server_default="activa")
    # NULL actividad_codigo = pending (legacy company that has not declared its activity yet).
    # `sector` above is deprecated free text, superseded by these two.
    actividad_codigo: Mapped[str | None] = mapped_column(
        String(4), ForeignKey("sector_actividad.codigo", name="fk_empresa_actividad")
    )
    actividad_otra: Mapped[str | None] = mapped_column(String(200))

    @property
    def actividad_pendiente(self) -> bool:
        return self.actividad_codigo is None

    @property
    def ofrecer_ia(self) -> bool:
        """`OTRA` has no CNAE section to match on: the AI offer is the way to place the company."""
        return self.actividad_codigo == CODIGO_ACTIVIDAD_OTRA
