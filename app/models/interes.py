from sqlalchemy import ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

# (codigo, nombre) of the 8 "what do you want to invest in" interests, in display order.
# The migration seeds exactly these; `tests/test_empresa_perfil.py` checks both agree: change them together.
INTERESES = (
    ("erp", "ERP y gestión"),
    ("automatizacion", "Automatización"),
    ("energia", "Energía"),
    ("ciberseguridad", "Ciberseguridad"),
    ("exportacion", "Exportación"),
    ("formacion", "Formación"),
    ("id_producto", "I+D de producto"),
    ("contratacion", "Contratación"),
)
CODIGOS_INTERES = tuple(codigo for codigo, _ in INTERESES)


class Interes(Base):
    """Static, read-only catalog of investment interests. No audit columns: only migrations write here."""

    __tablename__ = "interes"

    codigo: Mapped[str] = mapped_column(String(30), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    orden: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class EmpresaInteres(Base, AuditMixin):
    """Interests a company declared (feeds the affinity score). Replaced as a whole on every update."""

    __tablename__ = "empresa_interes"

    empresa_id: Mapped[int] = mapped_column(
        ForeignKey("empresa.id", ondelete="CASCADE", name="fk_empresa_interes_empresa"), primary_key=True
    )
    interes_codigo: Mapped[str] = mapped_column(
        ForeignKey("interes.codigo", name="fk_empresa_interes_interes"), primary_key=True
    )
