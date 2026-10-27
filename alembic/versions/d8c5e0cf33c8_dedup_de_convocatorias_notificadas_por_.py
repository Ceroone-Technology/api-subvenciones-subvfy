"""dedup de convocatorias notificadas por alerta

Dos cambios para que el motor de alertas no avise dos veces de la misma
convocatoria (Hito 4, tarea 3):

1. `alerta_ejecucion_convocatoria.alerta_id`, con `UNIQUE(alerta_id,
   convocatoria_id)`. Está desnormalizado a propósito —se podría deducir vía
   `alerta_ejecucion`— porque es lo que permite que **la garantía la dé
   Postgres**: con `ON CONFLICT DO NOTHING ... RETURNING`, lo devuelto es
   exactamente lo insertado, así que dos ciclos simultáneos no pueden registrar
   la misma novedad dos veces. Sin la columna, el UNIQUE solo cubre una
   ejecución y la protección entre ejecuciones quedaría en manos del código.
2. `estado_envio` admite `pendiente_envio`: hay novedades registradas pero el
   aviso todavía no ha salido. El envío es otra funcionalidad; marcar
   `enviado` sin haber enviado nada sería mentir en el historial.

Autogenerate detectó la columna, el UNIQUE y la FK, pero **no compara los
CHECK**, así que ese paso va a mano. También se sacó el NOT NULL del
`add_column`: con filas existentes, añadir la columna ya obligatoria falla.

Revision ID: d8c5e0cf33c8
Revises: e4a19c7d2b58
Create Date: 2026-09-25 09:55:26.689836

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8c5e0cf33c8"
down_revision: Union[str, None] = "e4a19c7d2b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ESTADOS_ANTERIORES = "'enviado', 'sin_novedades', 'error'"
ESTADOS_NUEVOS = "'pendiente_envio', 'enviado', 'sin_novedades', 'error'"


def upgrade() -> None:
    # Nullable primero: si la tabla ya tuviera filas, un NOT NULL directo
    # fallaría antes de poder rellenarlas.
    op.add_column("alerta_ejecucion_convocatoria", sa.Column("alerta_id", sa.BigInteger(), nullable=True))
    op.execute(
        """
        UPDATE alerta_ejecucion_convocatoria AS aec
           SET alerta_id = ae.alerta_id
          FROM alerta_ejecucion AS ae
         WHERE ae.id = aec.alerta_ejecucion_id
        """
    )
    # El UNIQUE no se puede crear si el histórico ya repetía una convocatoria
    # en dos ejecuciones de la misma alerta. Hoy la tabla está vacía en todos
    # los entornos, pero la migración no puede darlo por hecho: se conserva la
    # fila más antigua de cada par.
    op.execute(
        """
        DELETE FROM alerta_ejecucion_convocatoria
         WHERE id NOT IN (
               SELECT MIN(id)
                 FROM alerta_ejecucion_convocatoria
                GROUP BY alerta_id, convocatoria_id
         )
        """
    )
    op.alter_column("alerta_ejecucion_convocatoria", "alerta_id", nullable=False)
    op.create_foreign_key(
        "fk_alerta_ejecucion_convocatoria_alerta",
        "alerta_ejecucion_convocatoria",
        "alerta",
        ["alerta_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_alerta_convocatoria_notificada",
        "alerta_ejecucion_convocatoria",
        ["alerta_id", "convocatoria_id"],
    )

    op.drop_constraint("ck_alerta_ejecucion_estado", "alerta_ejecucion", type_="check")
    op.create_check_constraint(
        "ck_alerta_ejecucion_estado", "alerta_ejecucion", f"estado_envio IN ({ESTADOS_NUEVOS})"
    )


def downgrade() -> None:
    # Las ejecuciones que quedaron en pendiente_envio no cabrían en el CHECK
    # anterior: se recolocan en el estado que las describe sin el matiz nuevo.
    op.execute("UPDATE alerta_ejecucion SET estado_envio = 'enviado' WHERE estado_envio = 'pendiente_envio'")
    op.drop_constraint("ck_alerta_ejecucion_estado", "alerta_ejecucion", type_="check")
    op.create_check_constraint(
        "ck_alerta_ejecucion_estado", "alerta_ejecucion", f"estado_envio IN ({ESTADOS_ANTERIORES})"
    )

    op.drop_constraint("uq_alerta_convocatoria_notificada", "alerta_ejecucion_convocatoria", type_="unique")
    op.drop_constraint(
        "fk_alerta_ejecucion_convocatoria_alerta", "alerta_ejecucion_convocatoria", type_="foreignkey"
    )
    op.drop_column("alerta_ejecucion_convocatoria", "alerta_id")
