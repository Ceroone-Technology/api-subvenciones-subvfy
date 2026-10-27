from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

NIVELES_ADMINISTRACION = ("estado", "ccaa", "local", "otros")

# Full-text search: `es_unaccent` is the text search configuration created by
# migration `bdns_local_sync` (Spanish, plus the `unaccent` mapping when the
# extension is available). A regconfig mapping is IMMUTABLE, so it can feed a
# generated column; calling `unaccent()` directly could not.
CONFIG_BUSQUEDA = "es_unaccent"
EXPRESION_BUSQUEDA = (
    f"setweight(to_tsvector('{CONFIG_BUSQUEDA}', titulo), 'A') || "
    f"setweight(to_tsvector('{CONFIG_BUSQUEDA}', coalesce(organo_convocante, '') || ' ' || "
    "coalesce(administracion, '') || ' ' || coalesce(finalidad, '')), 'B')"
)


class Convocatoria(Base, AuditMixin):
    """Local copy of the BDNS grants, kept up to date by `sync-bdns`.

    Since `bdns-local-sync` this table is what the app searches (BDNS stays the
    upstream source of truth). It is also what favorites, alerts and analyses
    point to with an FK, so rows are never hard-deleted: a grant that BDNS
    stops returning gets `removed_at`.
    """

    __tablename__ = "convocatoria"
    __table_args__ = (
        CheckConstraint(f"nivel_administracion IN {NIVELES_ADMINISTRACION}", name="ck_convocatoria_nivel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # unique=True ya genera el índice; el ix_convocatoria_codigo_bdns
    # separado de schema-subvfy.sql era redundante sobre la misma columna.
    codigo_bdns: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    titulo: Mapped[str] = mapped_column(String(500), nullable=False)
    nivel_administracion: Mapped[str | None] = mapped_column(String(20))
    administracion: Mapped[str | None] = mapped_column(String(200))
    organo_convocante: Mapped[str | None] = mapped_column(String(300))
    fecha_registro: Mapped[date | None] = mapped_column(Date)
    url_portal_oficial: Mapped[str | None] = mapped_column(String(500))
    financiada_mrr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    sincronizado_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Added by `bdns-local-sync` ---
    # BDNS internal numeric id (`codigo_bdns` is the public code).
    bdns_id: Mapped[int | None] = mapped_column(Integer)
    fecha_inicio_solicitud: Mapped[date | None] = mapped_column(Date)
    fecha_fin_solicitud: Mapped[date | None] = mapped_column(Date)
    abierto_bdns: Mapped[bool | None] = mapped_column(Boolean)
    # Derived at ingest from BDNS `textInicio`/`textFin`; estado itself is
    # computed in SQL because a stored value would go stale daily.
    plazo_permanente: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    presupuesto_total: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    tipo_convocatoria: Mapped[str | None] = mapped_column(String(100))
    finalidad: Mapped[str | None] = mapped_column(String(300))
    url_bases_reguladoras: Mapped[str | None] = mapped_column(String(1000))
    sede_electronica: Mapped[str | None] = mapped_column(String(1000))
    # BDNS ids of the matched node plus its ancestors (filter: `col && ARRAY[...]`).
    organo_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list, server_default=text("'{}'::integer[]")
    )
    region_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list, server_default=text("'{}'::integer[]")
    )
    beneficiarios: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    sectores: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    # Compact detail: documents, instruments, funds, objectives, region labels, bases text.
    detalle: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # sha256 hex of the mapped content; an upsert is skipped when it is unchanged.
    contenido_hash: Mapped[str | None] = mapped_column(CHAR(64))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set once, when the first full detail is stored (the alerts window, D10).
    disponible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detalle_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Generated by the database; deferred so entity loads do not drag the vector along.
    busqueda: Mapped[Any] = mapped_column(
        TSVECTOR, Computed(EXPRESION_BUSQUEDA, persisted=True), nullable=True, deferred=True
    )


# Defined after the class so the ordered/partial ones can use the table columns.
_c = Convocatoria.__table__.c
Index(
    "ix_convocatoria_registro",
    _c.fecha_registro.desc(),
    _c.id.desc(),
    postgresql_where=_c.removed_at.is_(None),
)
Index("ix_convocatoria_fin", _c.fecha_fin_solicitud)
Index("ix_convocatoria_inicio", _c.fecha_inicio_solicitud)
Index("ix_convocatoria_busqueda", _c.busqueda, postgresql_using="gin")
Index("ix_convocatoria_organo_ids", _c.organo_ids, postgresql_using="gin")
Index("ix_convocatoria_region_ids", _c.region_ids, postgresql_using="gin")
Index("ix_convocatoria_beneficiarios", _c.beneficiarios, postgresql_using="gin")
# Rolling refresh walks the oldest `detalle_sync_at` first; never-fetched rows lead.
Index("ix_convocatoria_refresco", _c.detalle_sync_at.asc().nulls_first())
Index("ix_convocatoria_disponible", _c.disponible_at)
Index("ix_convocatoria_presupuesto", _c.presupuesto_total.desc().nulls_last())
