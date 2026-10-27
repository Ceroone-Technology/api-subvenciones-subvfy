"""reintentos del motor de alertas

Una alerta cuya ejecución falla tiene que dejar rastro y no reintentarse a
ciegas en cada ciclo. Dos columnas en `alerta`:

- `fallos_consecutivos`: cuántas ejecuciones seguidas han fallado. Un éxito o
  un PATCH de la alerta lo ponen a cero.
- `proximo_reintento_at`: hasta cuándo no se vuelve a intentar (espera
  creciente, con tope).

No se reutiliza `ultima_ejecucion_at`: es la fecha `desde` de la consulta a la
BDNS, y avanzarla con un fallo perdería lo publicado entre medias.

No toca ningún CHECK: el estado `error` y `detalle_error` ya existían en
`alerta_ejecucion`. Escrita a mano por ser dos columnas simples.

Revision ID: a3f9d27c51b8
Revises: d8c5e0cf33c8
Create Date: 2026-09-30 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f9d27c51b8"
down_revision: Union[str, None] = "d8c5e0cf33c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default para que las filas existentes queden en 0 sin UPDATE.
    op.add_column(
        "alerta",
        sa.Column("fallos_consecutivos", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("alerta", sa.Column("proximo_reintento_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("alerta", "proximo_reintento_at")
    op.drop_column("alerta", "fallos_consecutivos")
