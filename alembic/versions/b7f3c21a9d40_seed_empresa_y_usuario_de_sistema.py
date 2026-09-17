"""seed empresa y usuario de sistema

Cierra lo que dejó pendiente `0db12626fe94`: la empresa y el usuario "de
sistema" a los que apuntarán `created_by`/`updated_by` cuando quien escriba
no sea una persona sino un proceso automático (sync de convocatorias desde
la BDNS, ejecución de alertas, análisis IA en batch).

Tres decisiones deliberadas sobre este usuario:

- Nace en estado `bloqueado` y con una contraseña aleatoria que nadie
  conoce (ni siquiera queda registrada). No es una cuenta para entrar: es
  una identidad para firmar filas. El login y la dependencia de
  autenticación rechazan cualquier estado distinto de `activo`, así que no
  hay puerta trasera aquí aunque alguien acertara el hash.
- Su empresa queda en estado `inactiva`, para que no aparezca mezclada con
  clientes reales en los listados que filtran por `estado=activa`.
- Se referencia a sí mismo en `created_by`/`updated_by` (y firma su propia
  empresa). Es el único caso legítimo del ciclo rol/empresa/usuario, y de
  paso confirma que las FKs circulares añadidas con `op.create_foreign_key()`
  en `827c98b6a656` existen de verdad en la base de datos.

El primer admin real NO se crea aquí — no tendría sentido dejar una
contraseña conocida en el repositorio. Se crea con:

    docker compose exec api python -m app.cli crear-admin

Revision ID: b7f3c21a9d40
Revises: 0db12626fe94
Create Date: 2026-09-17 04:40:00.000000

"""

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.security import hashear_password

# revision identifiers, used by Alembic.
revision: str = "b7f3c21a9d40"
down_revision: Union[str, None] = "0db12626fe94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NIF_SISTEMA = "SISTEMA"
EMAIL_SISTEMA = "sistema@subvfy.es"


def upgrade() -> None:
    conexion = op.get_bind()

    empresa_id = conexion.execute(
        sa.text(
            "INSERT INTO empresa (razon_social, nif, descripcion, estado) "
            "VALUES (:razon_social, :nif, :descripcion, 'inactiva') RETURNING id"
        ),
        {
            "razon_social": "Subvfy (sistema)",
            "nif": NIF_SISTEMA,
            "descripcion": "Identidad interna para la auditoría de procesos automáticos. No es un cliente.",
        },
    ).scalar_one()

    rol_admin_id = conexion.execute(
        sa.text("SELECT id FROM rol WHERE codigo = 'admin'")
    ).scalar_one()

    usuario_id = conexion.execute(
        sa.text(
            "INSERT INTO usuario (empresa_id, rol_id, nombre, apellidos, email, password_hash, estado) "
            "VALUES (:empresa_id, :rol_id, 'Sistema', 'Subvfy', :email, :password_hash, 'bloqueado') "
            "RETURNING id"
        ),
        {
            "empresa_id": empresa_id,
            "rol_id": rol_admin_id,
            "email": EMAIL_SISTEMA,
            # Aleatoria y desechada: la cuenta no es para iniciar sesión.
            "password_hash": hashear_password(secrets.token_urlsafe(48)),
        },
    ).scalar_one()

    for tabla, fila_id in (("empresa", empresa_id), ("usuario", usuario_id)):
        conexion.execute(
            sa.text(f"UPDATE {tabla} SET created_by = :uid, updated_by = :uid WHERE id = :id"),
            {"uid": usuario_id, "id": fila_id},
        )


def downgrade() -> None:
    conexion = op.get_bind()
    # Primero se sueltan las autorreferencias: si no, el DELETE del usuario
    # choca contra las FKs de auditoría que él mismo firma.
    conexion.execute(
        sa.text(
            "UPDATE empresa SET created_by = NULL, updated_by = NULL WHERE nif = :nif"
        ),
        {"nif": NIF_SISTEMA},
    )
    conexion.execute(
        sa.text(
            "UPDATE usuario SET created_by = NULL, updated_by = NULL WHERE email = :email"
        ),
        {"email": EMAIL_SISTEMA},
    )
    conexion.execute(sa.text("DELETE FROM usuario WHERE email = :email"), {"email": EMAIL_SISTEMA})
    conexion.execute(sa.text("DELETE FROM empresa WHERE nif = :nif"), {"nif": NIF_SISTEMA})
