"""seed catalogo de roles

Datos semilla mínimos: el catálogo de roles (admin/gestor/usuario), tal
como lo deja pendiente el propio schema-subvfy.sql. El usuario y la
empresa "de sistema" para auditoría de procesos automáticos (sync de
convocatorias, ejecución de alertas, análisis IA batch) se sembrarán en el
Hito 2, Funcionalidad 4 (Autenticación real), cuando exista el hashing de
contraseña para crear ese usuario correctamente.

Revision ID: 0db12626fe94
Revises: 827c98b6a656
Create Date: 2026-09-17 03:51:46.062884

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0db12626fe94'
down_revision: Union[str, None] = '827c98b6a656'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLES = (
    ("admin", "Administrador"),
    ("gestor", "Gestor"),
    ("usuario", "Usuario"),
)

rol_table = sa.table(
    "rol",
    sa.column("codigo", sa.String),
    sa.column("nombre", sa.String),
)


def upgrade() -> None:
    op.bulk_insert(rol_table, [{"codigo": codigo, "nombre": nombre} for codigo, nombre in ROLES])


def downgrade() -> None:
    codigos_sql = ", ".join(f"'{codigo}'" for codigo, _ in ROLES)
    op.execute(f"DELETE FROM rol WHERE codigo IN ({codigos_sql})")
