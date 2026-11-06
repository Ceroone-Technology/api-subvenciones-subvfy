"""bdns local sync

Schema for keeping a local copy of the BDNS (change `bdns-local-sync`, PR1):

- `convocatoria` gains the sync fields (dates, budget, multi-valued filters as
  arrays, compact detail, sync timestamps) and a generated `busqueda` tsvector.
- New `catalogo_bdns` (organ and region trees) and `sync_run` (one row per run).
- Text search configuration `es_unaccent`.

Written by hand, because Alembic autogenerate does not see what matters here:

- CHECK constraints are not compared, so they are declared explicitly.
- The audit FKs of the two new tables are added with `op.create_foreign_key`
  at the end of `upgrade` (and dropped first in `downgrade`), the same pattern
  as `827c98b6a656`: `use_alter=True` inside `create_table` emits no ALTER.
- The generated column and the text search configuration are raw SQL.

`es_unaccent` is a COPY of `spanish`, and the `unaccent` mapping is added only
when the extension is available (`pg_available_extensions`), so the same name
keeps working, without accent folding, where it is not. A regconfig mapping is
IMMUTABLE, which a generated column requires; calling `unaccent()` is not.

The downgrade drops everything this migration created. The `unaccent`
extension is left installed (other things may use it). The existing
`ck_convocatoria_nivel` is untouched.

Revision ID: c6e4a8b2f10d
Revises: a3f9d27c51b8
Create Date: 2026-10-22 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c6e4a8b2f10d"
down_revision: Union[str, None] = "a3f9d27c51b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMPTY_INT_ARRAY = sa.text("'{}'::integer[]")
EMPTY_TEXT_ARRAY = sa.text("'{}'::text[]")

BUSQUEDA_EXPRESSION = (
    "setweight(to_tsvector('es_unaccent', titulo), 'A') || "
    "setweight(to_tsvector('es_unaccent', coalesce(organo_convocante, '') || ' ' || "
    "coalesce(administracion, '') || ' ' || coalesce(finalidad, '')), 'B')"
)

# One statement per op.execute: asyncpg cannot run several in a prepared statement.
CREATE_TEXT_SEARCH_CONFIG = "CREATE TEXT SEARCH CONFIGURATION es_unaccent (COPY = spanish)"

ADD_UNACCENT_MAPPING = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'unaccent') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS unaccent;
            ALTER TEXT SEARCH CONFIGURATION es_unaccent
                ALTER MAPPING FOR hword, hword_part, word WITH unaccent, spanish_stem;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'unaccent not installable here: es_unaccent keeps accents';
        END;
    END IF;
END
$$;
"""

# Tables that get the audit FKs (created_by/updated_by -> usuario.id) by hand.
AUDIT_TABLES = ("catalogo_bdns", "sync_run")


def upgrade() -> None:
    op.execute(CREATE_TEXT_SEARCH_CONFIG)
    op.execute(ADD_UNACCENT_MAPPING)

    # --- convocatoria: new columns -------------------------------------------------
    op.add_column("convocatoria", sa.Column("bdns_id", sa.Integer(), nullable=True))
    op.add_column("convocatoria", sa.Column("fecha_inicio_solicitud", sa.Date(), nullable=True))
    op.add_column("convocatoria", sa.Column("fecha_fin_solicitud", sa.Date(), nullable=True))
    op.add_column("convocatoria", sa.Column("abierto_bdns", sa.Boolean(), nullable=True))
    op.add_column(
        "convocatoria",
        sa.Column("plazo_permanente", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("convocatoria", sa.Column("presupuesto_total", sa.Numeric(16, 2), nullable=True))
    op.add_column("convocatoria", sa.Column("tipo_convocatoria", sa.String(length=100), nullable=True))
    op.add_column("convocatoria", sa.Column("finalidad", sa.String(length=300), nullable=True))
    op.add_column("convocatoria", sa.Column("url_bases_reguladoras", sa.String(length=1000), nullable=True))
    op.add_column("convocatoria", sa.Column("sede_electronica", sa.String(length=1000), nullable=True))
    op.add_column(
        "convocatoria",
        sa.Column("organo_ids", postgresql.ARRAY(sa.Integer()), server_default=EMPTY_INT_ARRAY, nullable=False),
    )
    op.add_column(
        "convocatoria",
        sa.Column("region_ids", postgresql.ARRAY(sa.Integer()), server_default=EMPTY_INT_ARRAY, nullable=False),
    )
    op.add_column(
        "convocatoria",
        sa.Column("beneficiarios", postgresql.ARRAY(sa.Text()), server_default=EMPTY_TEXT_ARRAY, nullable=False),
    )
    op.add_column(
        "convocatoria",
        sa.Column("sectores", postgresql.ARRAY(sa.Text()), server_default=EMPTY_TEXT_ARRAY, nullable=False),
    )
    op.add_column("convocatoria", sa.Column("detalle", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("convocatoria", sa.Column("contenido_hash", sa.CHAR(length=64), nullable=True))
    op.add_column("convocatoria", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("convocatoria", sa.Column("disponible_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("convocatoria", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("convocatoria", sa.Column("detalle_sync_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("convocatoria", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    # Generated column: raw SQL so the expression is exactly the one in the design.
    op.execute(
        f"ALTER TABLE convocatoria ADD COLUMN busqueda tsvector GENERATED ALWAYS AS ({BUSQUEDA_EXPRESSION}) STORED"
    )

    # --- convocatoria: indexes -----------------------------------------------------
    op.create_index(
        "ix_convocatoria_registro",
        "convocatoria",
        [sa.text("fecha_registro DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_index("ix_convocatoria_fin", "convocatoria", ["fecha_fin_solicitud"])
    op.create_index("ix_convocatoria_inicio", "convocatoria", ["fecha_inicio_solicitud"])
    op.create_index("ix_convocatoria_busqueda", "convocatoria", ["busqueda"], postgresql_using="gin")
    op.create_index("ix_convocatoria_organo_ids", "convocatoria", ["organo_ids"], postgresql_using="gin")
    op.create_index("ix_convocatoria_region_ids", "convocatoria", ["region_ids"], postgresql_using="gin")
    op.create_index("ix_convocatoria_beneficiarios", "convocatoria", ["beneficiarios"], postgresql_using="gin")
    op.create_index("ix_convocatoria_refresco", "convocatoria", [sa.text("detalle_sync_at ASC NULLS FIRST")])
    op.create_index("ix_convocatoria_disponible", "convocatoria", ["disponible_at"])
    op.create_index("ix_convocatoria_presupuesto", "convocatoria", [sa.text("presupuesto_total DESC NULLS LAST")])

    # --- catalogo_bdns -------------------------------------------------------------
    op.create_table(
        "catalogo_bdns",
        sa.Column("tipo", sa.String(length=10), nullable=False),
        sa.Column("bdns_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("padre_bdns_id", sa.Integer(), nullable=True),
        sa.Column("descripcion", sa.String(length=500), nullable=False),
        sa.Column("codigo", sa.String(length=20), nullable=True),
        sa.Column("nivel_administracion", sa.String(length=20), nullable=True),
        sa.Column("ruta_ids", postgresql.ARRAY(sa.Integer()), server_default=EMPTY_INT_ARRAY, nullable=False),
        sa.Column("ruta_norm", sa.Text(), nullable=True),
        sa.Column("sincronizado_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("tipo", "bdns_id"),
        sa.CheckConstraint("tipo IN ('organo', 'region')", name="ck_catalogo_bdns_tipo"),
        sa.CheckConstraint(
            "nivel_administracion IN ('estado', 'ccaa', 'local', 'otros')", name="ck_catalogo_bdns_nivel"
        ),
    )

    # --- sync_run ------------------------------------------------------------------
    op.create_table(
        "sync_run",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("modo", sa.String(length=15), nullable=False),
        sa.Column("estado", sa.String(length=10), server_default="running", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ventana_desde", sa.Date(), nullable=True),
        sa.Column("ventana_hasta", sa.Date(), nullable=True),
        sa.Column("listadas", sa.Integer(), server_default="0", nullable=False),
        sa.Column("detalles", sa.Integer(), server_default="0", nullable=False),
        sa.Column("insertadas", sa.Integer(), server_default="0", nullable=False),
        sa.Column("actualizadas", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retiradas", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errores", sa.Integer(), server_default="0", nullable=False),
        sa.Column("organos_sin_resolver", sa.Integer(), server_default="0", nullable=False),
        sa.Column("peticiones_bdns", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_detalle", sa.String(length=500), nullable=True),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("modo IN ('incremental', 'backfill')", name="ck_sync_run_modo"),
        sa.CheckConstraint("estado IN ('running', 'ok', 'partial', 'failed')", name="ck_sync_run_estado"),
    )
    op.create_index("ix_sync_run_modo_estado", "sync_run", ["modo", "estado", sa.text("finished_at DESC")])

    # Audit FKs, at the end: create_table() would not emit them (see docstring).
    for tabla in AUDIT_TABLES:
        op.create_foreign_key(f"fk_{tabla}_created_by", tabla, "usuario", ["created_by"], ["id"])
        op.create_foreign_key(f"fk_{tabla}_updated_by", tabla, "usuario", ["updated_by"], ["id"])


def downgrade() -> None:
    for tabla in reversed(AUDIT_TABLES):
        op.drop_constraint(f"fk_{tabla}_updated_by", tabla, type_="foreignkey")
        op.drop_constraint(f"fk_{tabla}_created_by", tabla, type_="foreignkey")

    op.drop_index("ix_sync_run_modo_estado", table_name="sync_run")
    op.drop_table("sync_run")
    op.drop_table("catalogo_bdns")

    for indice in (
        "ix_convocatoria_presupuesto",
        "ix_convocatoria_disponible",
        "ix_convocatoria_refresco",
        "ix_convocatoria_beneficiarios",
        "ix_convocatoria_region_ids",
        "ix_convocatoria_organo_ids",
        "ix_convocatoria_busqueda",
        "ix_convocatoria_inicio",
        "ix_convocatoria_fin",
        "ix_convocatoria_registro",
    ):
        op.drop_index(indice, table_name="convocatoria")

    # `busqueda` depends on the es_unaccent configuration: column first.
    for columna in (
        "busqueda",
        "removed_at",
        "detalle_sync_at",
        "last_seen_at",
        "disponible_at",
        "first_seen_at",
        "contenido_hash",
        "detalle",
        "sectores",
        "beneficiarios",
        "region_ids",
        "organo_ids",
        "sede_electronica",
        "url_bases_reguladoras",
        "finalidad",
        "tipo_convocatoria",
        "presupuesto_total",
        "plazo_permanente",
        "abierto_bdns",
        "fecha_fin_solicitud",
        "fecha_inicio_solicitud",
        "bdns_id",
    ):
        op.drop_column("convocatoria", columna)

    op.execute("DROP TEXT SEARCH CONFIGURATION es_unaccent")
