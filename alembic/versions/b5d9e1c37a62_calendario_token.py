"""calendar subscription token

Plan N6: `usuario.calendario_token`, the secret in the URL of the personal ICS feed (`/calendario/feed/{token}.ics`).
Nullable (created on first use) and UNIQUE; the CHECK refuses anything shorter than 32 characters so a guessable
value cannot be stored by hand. Written by hand: Alembic does not compare CHECKs.

Revision ID: b5d9e1c37a62
Revises: a8e2c5d91f47
Create Date: 2026-11-07 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5d9e1c37a62"
down_revision: Union[str, None] = "a8e2c5d91f47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("usuario", sa.Column("calendario_token", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_usuario_calendario_token", "usuario", ["calendario_token"])
    op.create_check_constraint(
        "ck_usuario_calendario_token", "usuario", "calendario_token IS NULL OR length(calendario_token) >= 32"
    )


def downgrade() -> None:
    op.drop_constraint("ck_usuario_calendario_token", "usuario", type_="check")
    op.drop_constraint("uq_usuario_calendario_token", "usuario", type_="unique")
    op.drop_column("usuario", "calendario_token")
