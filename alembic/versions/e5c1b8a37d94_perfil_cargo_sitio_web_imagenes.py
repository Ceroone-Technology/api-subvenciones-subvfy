"""profile: cargo, sitio_web, avatar and logo

`usuario.cargo` (varchar 100), `empresa.sitio_web` (varchar 200, http/https only) and the sha256 of the stored
avatar/logo (`usuario.avatar_sha256`, `empresa.logo_sha256`; the image itself lives in the content-addressed store).
All nullable. The CHECKs are written by hand (Alembic does not compare them) and dropped in the downgrade together
with the columns; the downgrade loses these values by design (they did not exist before this revision).

Revision ID: e5c1b8a37d94
Revises: a9d3e6b14f72
Create Date: 2026-11-07 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5c1b8a37d94"
down_revision: Union[str, None] = "a9d3e6b14f72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SITIO_WEB = (
    "sitio_web IS NULL OR (length(sitio_web) <= 200 "
    "AND sitio_web ~* '^https?://[^[:space:]/?#]+([/?#][^[:space:]]*)?$')"
)


def upgrade() -> None:
    op.add_column("usuario", sa.Column("cargo", sa.String(length=100), nullable=True))
    op.add_column("usuario", sa.Column("avatar_sha256", sa.String(length=64), nullable=True))
    op.create_check_constraint("ck_usuario_cargo", "usuario", "cargo IS NULL OR length(trim(cargo)) > 0")
    op.create_check_constraint(
        "ck_usuario_avatar_sha256", "usuario", "avatar_sha256 IS NULL OR avatar_sha256 ~ '^[0-9a-f]{64}$'"
    )
    op.add_column("empresa", sa.Column("sitio_web", sa.String(length=200), nullable=True))
    op.add_column("empresa", sa.Column("logo_sha256", sa.String(length=64), nullable=True))
    op.create_check_constraint("ck_empresa_sitio_web", "empresa", SITIO_WEB)
    op.create_check_constraint(
        "ck_empresa_logo_sha256", "empresa", "logo_sha256 IS NULL OR logo_sha256 ~ '^[0-9a-f]{64}$'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_empresa_logo_sha256", "empresa", type_="check")
    op.drop_constraint("ck_empresa_sitio_web", "empresa", type_="check")
    op.drop_column("empresa", "logo_sha256")
    op.drop_column("empresa", "sitio_web")
    op.drop_constraint("ck_usuario_avatar_sha256", "usuario", type_="check")
    op.drop_constraint("ck_usuario_cargo", "usuario", type_="check")
    op.drop_column("usuario", "avatar_sha256")
    op.drop_column("usuario", "cargo")
