"""CNAE sections on each grant

Change `empresa-actividad-ia`, PR B2: `convocatoria.cnae_secciones char(1)[]` (+ GIN index), the
letters derived from the BDNS sector codes (`app.services.cnae`).

No SQL backfill: the stored `sectores` are descriptions without codes and the BDNS `/sectores`
catalog is the agricultural-product list, not CNAE, so nothing in the database can resolve them.
Existing rows start with `{}`; the regular detail refresh fills them (the sections enter
`contenido_hash`) and `python -m app.cli rellenar-cnae` speeds it up. Until then the search
filter leaves them out (a grant with sectors but no sections is not "open to every sector").

Revision ID: b3e8f5a2d917
Revises: a7d2e9c41b36
Create Date: 2026-11-06 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "b3e8f5a2d917"
down_revision: Union[str, None] = "a7d2e9c41b36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "convocatoria",
        sa.Column(
            "cnae_secciones", ARRAY(sa.CHAR(1)), nullable=False, server_default=sa.text("'{}'::char(1)[]")
        ),
    )
    op.create_index(
        "ix_convocatoria_cnae_secciones", "convocatoria", ["cnae_secciones"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_convocatoria_cnae_secciones", table_name="convocatoria")
    op.drop_column("convocatoria", "cnae_secciones")
