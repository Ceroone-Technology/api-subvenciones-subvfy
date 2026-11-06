"""discarded grants

Plan N4: `descarte(usuario_id, convocatoria_id)`, the "No me interesa" list of each user. New table only, nothing
existing changes. Written by hand like the other recent revisions: the audit FKs are added with
`op.create_foreign_key` after `create_table` (pattern of `827c98b6a656`) and dropped first in the downgrade.
The downgrade drops the table; the discards are lost by design (they did not exist before this revision).

Revision ID: a9d3e6b14f72
Revises: c3a7f2d84e19
Create Date: 2026-11-07 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9d3e6b14f72"
down_revision: Union[str, None] = "c3a7f2d84e19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "descarte",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("convocatoria_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuario.id"], name="fk_descarte_usuario", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["convocatoria_id"], ["convocatoria.id"], name="fk_descarte_convocatoria", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("usuario_id", "convocatoria_id", name="uq_descarte_usuario_convocatoria"),
    )
    op.create_index("ix_descarte_usuario", "descarte", ["usuario_id"])
    op.create_foreign_key("fk_descarte_created_by", "descarte", "usuario", ["created_by"], ["id"])
    op.create_foreign_key("fk_descarte_updated_by", "descarte", "usuario", ["updated_by"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_descarte_updated_by", "descarte", type_="foreignkey")
    op.drop_constraint("fk_descarte_created_by", "descarte", type_="foreignkey")
    op.drop_index("ix_descarte_usuario", table_name="descarte")
    op.drop_table("descarte")
