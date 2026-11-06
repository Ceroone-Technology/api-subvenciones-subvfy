"""documents cache

Change `bdns-local-sync`, PR10: metadata of documents cached in our own storage
(`convocatoria_documento`) and access events (`documento_acceso`, user id only).

Written by hand: Alembic does not compare CHECKs, and the audit FKs are added
with `op.create_foreign_key` at the end of `upgrade` (pattern of `827c98b6a656`).
The downgrade drops both tables (events first) and everything they own.

Revision ID: f1b8d3a67c24
Revises: c6e4a8b2f10d
Create Date: 2026-11-06 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1b8d3a67c24"
down_revision: Union[str, None] = "c6e4a8b2f10d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLAS = ("convocatoria_documento", "documento_acceso")


def _auditoria() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "convocatoria_documento",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("convocatoria_id", sa.BigInteger(), nullable=False),
        sa.Column("bdns_documento_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("tamano", sa.BigInteger(), nullable=True),
        sa.Column("mime", sa.String(length=150), nullable=True),
        sa.Column("nombre_archivo", sa.String(length=300), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        *_auditoria(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["convocatoria_id"], ["convocatoria.id"], name="fk_convocatoria_documento_convocatoria"
        ),
        sa.UniqueConstraint("convocatoria_id", "bdns_documento_id", name="uq_convocatoria_documento_bdns"),
        sa.CheckConstraint(
            "(fetched_at IS NULL AND sha256 IS NULL AND tamano IS NULL) OR "
            "(fetched_at IS NOT NULL AND sha256 IS NOT NULL AND tamano IS NOT NULL)",
            name="ck_convocatoria_documento_fetched",
        ),
        sa.CheckConstraint("tamano IS NULL OR tamano >= 0", name="ck_convocatoria_documento_tamano"),
    )
    op.create_table(
        "documento_acceso",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("documento_id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("accedido_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("origen", sa.String(length=10), nullable=False),
        *_auditoria(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["documento_id"], ["convocatoria_documento.id"], name="fk_documento_acceso_documento"),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuario.id"], name="fk_documento_acceso_usuario"),
        sa.CheckConstraint("origen IN ('bdns', 'cache')", name="ck_documento_acceso_origen"),
    )
    op.create_index("ix_documento_acceso_documento", "documento_acceso", ["documento_id", sa.text("accedido_at DESC")])
    op.create_index("ix_documento_acceso_usuario", "documento_acceso", ["usuario_id"])

    for tabla in TABLAS:
        op.create_foreign_key(f"fk_{tabla}_created_by", tabla, "usuario", ["created_by"], ["id"])
        op.create_foreign_key(f"fk_{tabla}_updated_by", tabla, "usuario", ["updated_by"], ["id"])


def downgrade() -> None:
    for tabla in reversed(TABLAS):
        op.drop_constraint(f"fk_{tabla}_updated_by", tabla, type_="foreignkey")
        op.drop_constraint(f"fk_{tabla}_created_by", tabla, type_="foreignkey")
    op.drop_index("ix_documento_acceso_usuario", table_name="documento_acceso")
    op.drop_index("ix_documento_acceso_documento", table_name="documento_acceso")
    op.drop_table("documento_acceso")
    op.drop_table("convocatoria_documento")
