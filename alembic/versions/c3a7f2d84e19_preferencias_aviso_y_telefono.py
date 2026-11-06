"""notice preferences and user phone

Plan N9a and N11: the table `usuario_preferencia_aviso` (one row per user and kind of notice, only for what the user
saved) and `usuario.telefono` (optional). Written by hand: Alembic does not compare CHECKs, so the two new ones are
created here; the audit FKs of the new table are added with `op.create_foreign_key` at the end of `upgrade`.
The downgrade drops the table and the column with their constraints (the data is lost by design).

Revision ID: c3a7f2d84e19
Revises: b5d9e1c37a62
Create Date: 2026-11-07 11:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3a7f2d84e19"
down_revision: Union[str, None] = "b5d9e1c37a62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIPOS_AVISO = ("nuevas", "plazos", "alertas", "semanal", "seguimiento")
TELEFONO = r"telefono IS NULL OR telefono ~ '^\+?[0-9 ()\-.]{6,30}$'"


def upgrade() -> None:
    op.add_column("usuario", sa.Column("telefono", sa.String(length=30), nullable=True))
    op.create_check_constraint("ck_usuario_telefono", "usuario", TELEFONO)

    op.create_table(
        "usuario_preferencia_aviso",
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("email", sa.Boolean(), nullable=False),
        sa.Column("app", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("usuario_id", "tipo"),
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuario.id"], name="fk_usuario_preferencia_aviso_usuario", ondelete="CASCADE"
        ),
        sa.CheckConstraint(f"tipo IN {TIPOS_AVISO}", name="ck_usuario_preferencia_aviso_tipo"),
    )
    op.create_foreign_key(
        "fk_usuario_preferencia_aviso_created_by", "usuario_preferencia_aviso", "usuario", ["created_by"], ["id"]
    )
    op.create_foreign_key(
        "fk_usuario_preferencia_aviso_updated_by", "usuario_preferencia_aviso", "usuario", ["updated_by"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_usuario_preferencia_aviso_updated_by", "usuario_preferencia_aviso", type_="foreignkey")
    op.drop_constraint("fk_usuario_preferencia_aviso_created_by", "usuario_preferencia_aviso", type_="foreignkey")
    op.drop_table("usuario_preferencia_aviso")
    op.drop_constraint("ck_usuario_telefono", "usuario", type_="check")
    op.drop_column("usuario", "telefono")
