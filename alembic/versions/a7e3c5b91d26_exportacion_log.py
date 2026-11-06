"""export log

Every export of the grants list is logged (user, company, format, filters, rows). The daily limit per user is
counted from this table, so it holds across processes. New table only; the downgrade drops it (the log is lost by
design). CHECKs written by hand; audit FKs added after `create_table` (pattern of `827c98b6a656`).

Revision ID: a7e3c5b91d26
Revises: b4e8c1d97a36
Create Date: 2026-11-07 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7e3c5b91d26"
down_revision: Union[str, None] = "b4e8c1d97a36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exportacion_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("empresa_id", sa.BigInteger(), nullable=False),
        sa.Column("formato", sa.String(length=4), nullable=False),
        sa.Column("filtros", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("filas", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuario.id"], name="fk_exportacion_log_usuario", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["empresa_id"], ["empresa.id"], name="fk_exportacion_log_empresa", ondelete="CASCADE"
        ),
        sa.CheckConstraint("formato IN ('csv', 'xlsx')", name="ck_exportacion_log_formato"),
        sa.CheckConstraint("filas >= 0", name="ck_exportacion_log_filas"),
    )
    op.create_index("ix_exportacion_log_usuario_fecha", "exportacion_log", ["usuario_id", "created_at"])
    op.create_foreign_key("fk_exportacion_log_created_by", "exportacion_log", "usuario", ["created_by"], ["id"])
    op.create_foreign_key("fk_exportacion_log_updated_by", "exportacion_log", "usuario", ["updated_by"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_exportacion_log_updated_by", "exportacion_log", type_="foreignkey")
    op.drop_constraint("fk_exportacion_log_created_by", "exportacion_log", type_="foreignkey")
    op.drop_index("ix_exportacion_log_usuario_fecha", table_name="exportacion_log")
    op.drop_table("exportacion_log")
