from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin
from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA

TAMANOS_VALIDOS = ("autonomo", "micro", "pequena", "mediana", "grande")
ESTADOS_EMPRESA = ("activa", "inactiva")
# Annual turnover bands (optional profile field; some grants cap by turnover).
TRAMOS_FACTURACION = ("menos_100k", "100k_500k", "500k_2m", "2m_10m", "10m_50m", "mas_50m")
MAX_EMPLEADOS = 1_000_000
MAX_PROYECTO_PREVISTO = 2000


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
        CheckConstraint(f"facturacion_tramo IN {TRAMOS_FACTURACION}", name="ck_empresa_facturacion_tramo"),
        CheckConstraint(f"empleados IS NULL OR empleados BETWEEN 0 AND {MAX_EMPLEADOS}", name="ck_empresa_empleados"),
        CheckConstraint(
            f"proyecto_previsto IS NULL OR length(proyecto_previsto) <= {MAX_PROYECTO_PREVISTO}",
            name="ck_empresa_proyecto_previsto",
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
    # Extended profile (N2), all optional: they sharpen the affinity score and count towards completeness.
    facturacion_tramo: Mapped[str | None] = mapped_column(String(12))
    empleados: Mapped[int | None] = mapped_column(Integer)
    provincia: Mapped[str | None] = mapped_column(String(60))
    municipio: Mapped[str | None] = mapped_column(String(100))
    proyecto_previsto: Mapped[str | None] = mapped_column(Text)

    @property
    def actividad_pendiente(self) -> bool:
        return self.actividad_codigo is None

    @property
    def ofrecer_ia(self) -> bool:
        """`OTRA` has no CNAE section to match on: the AI offer is the way to place the company."""
        return self.actividad_codigo == CODIGO_ACTIVIDAD_OTRA
