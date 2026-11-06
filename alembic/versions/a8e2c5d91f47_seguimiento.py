"""follow-up board: seguimiento, seguimiento_tarea, seguimiento_evento

Plan N5a/N5b. One card per company and grant (`UNIQUE(empresa_id, convocatoria_id)`), four states, and a
`resultado` that only exists in `resuelta`.

Written by hand: Alembic does not compare CHECKs, so they are created here with their names and dropped with
the tables in the downgrade; the audit FKs are added with `op.create_foreign_key` at the end of `upgrade`.
The downgrade drops everything created here (the data is lost by design: the tables did not exist before).

Revision ID: a8e2c5d91f47
Revises: f7a3c9d15e82
Create Date: 2026-11-07 09:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8e2c5d91f47"
down_revision: Union[str, None] = "f7a3c9d15e82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLAS = ("seguimiento", "seguimiento_tarea", "seguimiento_evento")


def _auditoria() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "seguimiento",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("empresa_id", sa.BigInteger(), nullable=False),
        sa.Column("convocatoria_id", sa.BigInteger(), nullable=False),
        sa.Column("estado", sa.String(length=12), server_default="interesa", nullable=False),
        sa.Column("resultado", sa.String(length=10), nullable=True),
        sa.Column("responsable_usuario_id", sa.BigInteger(), nullable=True),
        sa.Column("fecha_presentacion", sa.Date(), nullable=True),
        *_auditoria(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresa.id"], name="fk_seguimiento_empresa", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["convocatoria_id"], ["convocatoria.id"], name="fk_seguimiento_convocatoria", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["responsable_usuario_id"], ["usuario.id"], name="fk_seguimiento_responsable", ondelete="SET NULL"
        ),
        sa.UniqueConstraint("empresa_id", "convocatoria_id", name="uq_seguimiento_empresa_convocatoria"),
        sa.CheckConstraint("estado IN ('interesa', 'preparando', 'presentada', 'resuelta')", name="ck_seguimiento_estado"),
        sa.CheckConstraint("resultado IN ('concedida', 'denegada')", name="ck_seguimiento_resultado"),
        sa.CheckConstraint("resultado IS NULL OR estado = 'resuelta'", name="ck_seguimiento_resultado_resuelta"),
    )
    op.create_index("ix_seguimiento_empresa_estado", "seguimiento", ["empresa_id", "estado"])
    op.create_index("ix_seguimiento_responsable", "seguimiento", ["responsable_usuario_id"])

    op.create_table(
        "seguimiento_tarea",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("seguimiento_id", sa.BigInteger(), nullable=False),
        sa.Column("titulo", sa.String(length=200), nullable=False),
        sa.Column("tipo", sa.String(length=10), server_default="tarea", nullable=False),
        sa.Column("fecha_limite", sa.Date(), nullable=True),
        sa.Column("hecha", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("asignado_a", sa.BigInteger(), nullable=True),
        *_auditoria(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["seguimiento_id"], ["seguimiento.id"], name="fk_seguimiento_tarea_seguimiento", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["asignado_a"], ["usuario.id"], name="fk_seguimiento_tarea_asignado", ondelete="SET NULL"
        ),
        sa.CheckConstraint("tipo IN ('documento', 'tarea')", name="ck_seguimiento_tarea_tipo"),
        sa.CheckConstraint("length(btrim(titulo)) > 0", name="ck_seguimiento_tarea_titulo"),
    )
    op.create_index("ix_seguimiento_tarea_seguimiento", "seguimiento_tarea", ["seguimiento_id"])
    op.create_index(
        "ix_seguimiento_tarea_fecha",
        "seguimiento_tarea",
        ["fecha_limite"],
        postgresql_where=sa.text("fecha_limite IS NOT NULL"),
    )

    op.create_table(
        "seguimiento_evento",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("seguimiento_id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=True),
        sa.Column("tipo", sa.String(length=10), nullable=False),
        sa.Column("estado_anterior", sa.String(length=12), nullable=True),
        sa.Column("estado_nuevo", sa.String(length=12), nullable=True),
        sa.Column("ocurrido_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_auditoria(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["seguimiento_id"], ["seguimiento.id"], name="fk_seguimiento_evento_seguimiento", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuario.id"], name="fk_seguimiento_evento_usuario", ondelete="SET NULL"
        ),
        sa.CheckConstraint("tipo IN ('creada', 'movida', 'asignada', 'resuelta')", name="ck_seguimiento_evento_tipo"),
    )
    op.create_index("ix_seguimiento_evento_seguimiento", "seguimiento_evento", ["seguimiento_id", "ocurrido_at"])

    for tabla in TABLAS:
        op.create_foreign_key(f"fk_{tabla}_created_by", tabla, "usuario", ["created_by"], ["id"])
        op.create_foreign_key(f"fk_{tabla}_updated_by", tabla, "usuario", ["updated_by"], ["id"])


def downgrade() -> None:
    for tabla in reversed(TABLAS):
        op.drop_constraint(f"fk_{tabla}_updated_by", tabla, type_="foreignkey")
        op.drop_constraint(f"fk_{tabla}_created_by", tabla, type_="foreignkey")
    op.drop_table("seguimiento_evento")
    op.drop_table("seguimiento_tarea")
    op.drop_table("seguimiento")
