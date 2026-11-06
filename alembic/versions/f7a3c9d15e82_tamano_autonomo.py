"""company size: add "autonomo"

The design offers "Autónomo" next to micro/pequeña/mediana. Stored code: `autonomo`.

Written by hand: Alembic does not compare CHECKs. The downgrade first moves the rows that use the value that
goes away (`autonomo` -> `micro`, the closest remaining size) or the old CHECK could not be created.

Revision ID: f7a3c9d15e82
Revises: e5c1d8a74b20
Create Date: 2026-11-06 20:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "f7a3c9d15e82"
down_revision: Union[str, None] = "e5c1d8a74b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ANTES = ("micro", "pequena", "mediana", "grande")
AHORA = ("autonomo", *ANTES)


def upgrade() -> None:
    op.drop_constraint("ck_empresa_tamano", "empresa", type_="check")
    op.create_check_constraint("ck_empresa_tamano", "empresa", f"tamano IN {AHORA}")


def downgrade() -> None:
    op.execute("UPDATE empresa SET tamano = 'micro' WHERE tamano = 'autonomo'")
    op.drop_constraint("ck_empresa_tamano", "empresa", type_="check")
    op.create_check_constraint("ck_empresa_tamano", "empresa", f"tamano IN {ANTES}")
