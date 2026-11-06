"""usuario.ultimo_acceso_previo_at: the access before the current session (plan N10)

Nullable timestamp. Login rolls `ultimo_acceso_at` into it when a new session starts, so the Inicio summary can
count what is new since the previous visit. The downgrade drops the column (the values did not exist before).

Revision ID: f2d6a8c13b75
Revises: e5c1b8a37d94
Create Date: 2026-11-07 15:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2d6a8c13b75"
down_revision: Union[str, None] = "e5c1b8a37d94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("usuario", sa.Column("ultimo_acceso_previo_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("usuario", "ultimo_acceso_previo_at")
