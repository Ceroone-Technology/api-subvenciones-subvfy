from sqlalchemy import SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# CNAE-2009 sections A-U plus `OTRA` (free text). The migration seeds exactly these, in this order.
SECCIONES_CNAE = tuple("ABCDEFGHIJKLMNOPQRSTU")
CODIGO_ACTIVIDAD_OTRA = "OTRA"
CODIGOS_ACTIVIDAD = (*SECCIONES_CNAE, CODIGO_ACTIVIDAD_OTRA)


class SectorActividad(Base):
    """Static, read-only catalog of company activities (CNAE sections + `OTRA`).

    `division_desde`/`division_hasta` are the CNAE 2-digit division range of the
    section (NULL for `OTRA`); grant matching uses them, so a finer level can be
    added later without a schema change. No audit columns: nothing writes here
    outside migrations.
    """

    __tablename__ = "sector_actividad"

    codigo: Mapped[str] = mapped_column(String(4), primary_key=True)
    descripcion: Mapped[str] = mapped_column(String(200), nullable=False)
    orden: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    division_desde: Mapped[int | None] = mapped_column(SmallInteger)
    division_hasta: Mapped[int | None] = mapped_column(SmallInteger)
    version_cnae: Mapped[str] = mapped_column(String(10), nullable=False)
