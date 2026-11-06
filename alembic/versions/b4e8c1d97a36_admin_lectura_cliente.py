"""admin_lectura_cliente: audit log of admin reads of client companies

New append-only table (who = `admin_usuario_id`, which company = `empresa_id`, when = `created_at`). Written by hand
like the other recent revisions: audit FKs are added with `op.create_foreign_key` after `create_table` and dropped
first in the downgrade. The downgrade drops the table (the log did not exist before).

Revision ID: b4e8c1d97a36
Revises: f2d6a8c13b75
Create Date: 2026-11-07 17:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4e8c1d97a36"
down_revision: Union[str, None] = "f2d6a8c13b75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_lectura_cliente",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("admin_usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("empresa_id", sa.BigInteger(), nullable=False),
        sa.Column("accion", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["admin_usuario_id"], ["usuario.id"], name="fk_admin_lectura_cliente_admin", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["empresa_id"], ["empresa.id"], name="fk_admin_lectura_cliente_empresa", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_admin_lectura_cliente_empresa", "admin_lectura_cliente", ["empresa_id", "created_at"])
    op.create_foreign_key(
        "fk_admin_lectura_cliente_created_by", "admin_lectura_cliente", "usuario", ["created_by"], ["id"]
    )
    op.create_foreign_key(
        "fk_admin_lectura_cliente_updated_by", "admin_lectura_cliente", "usuario", ["updated_by"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_admin_lectura_cliente_updated_by", "admin_lectura_cliente", type_="foreignkey")
    op.drop_constraint("fk_admin_lectura_cliente_created_by", "admin_lectura_cliente", type_="foreignkey")
    op.drop_index("ix_admin_lectura_cliente_empresa", table_name="admin_lectura_cliente")
    op.drop_table("admin_lectura_cliente")
