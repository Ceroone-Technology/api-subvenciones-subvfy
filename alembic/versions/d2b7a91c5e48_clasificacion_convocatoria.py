"""own grant classification

Plan N1: `convocatoria.tema` (dig, id, ene, int, emp, otro) and `convocatoria.gestiona_subvfy`, plus the
admin-editable table of exceptions `gestion_excepcion`. Existing rows start as `otro` / not managed: run
`python -m app.cli recalcular-clasificacion` after this migration (offline, no BDNS calls).

Written by hand: Alembic does not compare CHECKs, and the audit FKs of the new table are added with
`op.create_foreign_key` at the end of `upgrade` (pattern of `827c98b6a656`). The downgrade removes the table,
the indexes, the CHECK and both columns, in reverse order.

Revision ID: d2b7a91c5e48
Revises: c9a4f6e1d283
Create Date: 2026-11-06 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2b7a91c5e48"
down_revision: Union[str, None] = "c9a4f6e1d283"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TEMAS = ("dig", "id", "ene", "int", "emp", "otro")


def upgrade() -> None:
    op.add_column(
        "convocatoria", sa.Column("tema", sa.String(length=5), server_default="otro", nullable=False)
    )
    op.add_column(
        "convocatoria", sa.Column("gestiona_subvfy", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.create_check_constraint("ck_convocatoria_tema", "convocatoria", f"tema IN {TEMAS}")
    op.create_index("ix_convocatoria_tema", "convocatoria", ["tema"])
    op.create_index(
        "ix_convocatoria_gestiona", "convocatoria", ["id"], postgresql_where=sa.text("gestiona_subvfy IS true")
    )

    op.create_table(
        "gestion_excepcion",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("convocatoria_id", sa.BigInteger(), nullable=False),
        sa.Column("gestiona", sa.Boolean(), nullable=False),
        sa.Column("motivo", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["convocatoria_id"], ["convocatoria.id"], name="fk_gestion_excepcion_convocatoria"),
        sa.UniqueConstraint("convocatoria_id", name="uq_gestion_excepcion_convocatoria"),
    )
    op.create_foreign_key("fk_gestion_excepcion_created_by", "gestion_excepcion", "usuario", ["created_by"], ["id"])
    op.create_foreign_key("fk_gestion_excepcion_updated_by", "gestion_excepcion", "usuario", ["updated_by"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_gestion_excepcion_updated_by", "gestion_excepcion", type_="foreignkey")
    op.drop_constraint("fk_gestion_excepcion_created_by", "gestion_excepcion", type_="foreignkey")
    op.drop_table("gestion_excepcion")
    op.drop_index("ix_convocatoria_gestiona", table_name="convocatoria")
    op.drop_index("ix_convocatoria_tema", table_name="convocatoria")
    op.drop_constraint("ck_convocatoria_tema", "convocatoria", type_="check")
    op.drop_column("convocatoria", "gestiona_subvfy")
    op.drop_column("convocatoria", "tema")
