from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin
from app.models.convocatoria import NIVELES_ADMINISTRACION

TIPOS_CATALOGO = ("organo", "region")


class CatalogoBdns(Base, AuditMixin):
    """Local copy of the BDNS organ and region trees.

    Ids are BDNS ids and are never renumbered: rows are upserted by
    `(tipo, bdns_id)` and never deleted, because alerts store those ids.
    `ruta_ids` is the node plus its ancestors (what `convocatoria.organo_ids`
    / `region_ids` store) and `ruta_norm` the upper-cased, accent-stripped
    text path used to resolve the organ from the detail's `nivel1/2/3`.
    """

    __tablename__ = "catalogo_bdns"
    __table_args__ = (
        CheckConstraint(f"tipo IN {TIPOS_CATALOGO}", name="ck_catalogo_bdns_tipo"),
        CheckConstraint(f"nivel_administracion IN {NIVELES_ADMINISTRACION}", name="ck_catalogo_bdns_nivel"),
    )

    tipo: Mapped[str] = mapped_column(String(10), primary_key=True)
    bdns_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    padre_bdns_id: Mapped[int | None] = mapped_column(Integer)
    descripcion: Mapped[str] = mapped_column(String(500), nullable=False)
    # NUTS code for regions (e.g. "ES12").
    codigo: Mapped[str | None] = mapped_column(String(20))
    nivel_administracion: Mapped[str | None] = mapped_column(String(20))
    ruta_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list, server_default=text("'{}'::integer[]")
    )
    ruta_norm: Mapped[str | None] = mapped_column(Text)
    sincronizado_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
