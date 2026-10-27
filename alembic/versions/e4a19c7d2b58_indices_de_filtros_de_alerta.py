"""indices de filtros de alerta

El lado `alerta_id` de `alerta_organo`/`alerta_region` ya lo indexan sus
UNIQUE(alerta_id, x). Faltaba la búsqueda inversa, la que necesitará el motor
de alertas (Hito 4) al entrar una convocatoria nueva: qué alertas vigilan
este órgano o esta región.

Escrita a mano con el mismo contenido que daría autogenerate: solo dos
índices, sin tocar rol/empresa/usuario.

Revision ID: e4a19c7d2b58
Revises: b7f3c21a9d40
Create Date: 2026-09-21 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4a19c7d2b58"
down_revision: Union[str, None] = "b7f3c21a9d40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_alerta_organo_organo", "alerta_organo", ["organo_bdns_id"], unique=False)
    op.create_index("ix_alerta_region_region", "alerta_region", ["region_bdns_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_alerta_region_region", table_name="alerta_region")
    op.drop_index("ix_alerta_organo_organo", table_name="alerta_organo")
