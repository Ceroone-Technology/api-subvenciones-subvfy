"""extended company profile

Plan N2: `empresa.facturacion_tramo`, `empleados`, `provincia`, `municipio`, `proyecto_previsto`, the catalog
`interes` (8 interests, seeded here) and the link `empresa_interes`. All new columns are nullable: existing
companies simply have an incomplete profile.

Written by hand: Alembic does not compare CHECKs, so the three new ones are created here and dropped in the
downgrade; the audit FKs of `empresa_interes` are added with `op.create_foreign_key` at the end of `upgrade`
(pattern of `827c98b6a656`). The downgrade drops the link, the catalog, the CHECKs and the columns, in that order;
the data they held is lost by design (it did not exist before this revision).

Revision ID: e5c1d8a74b20
Revises: d2b7a91c5e48
Create Date: 2026-11-06 19:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5c1d8a74b20"
down_revision: Union[str, None] = "d2b7a91c5e48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TRAMOS_FACTURACION = ("menos_100k", "100k_500k", "500k_2m", "2m_10m", "10m_50m", "mas_50m")
INTERESES = [
    ("erp", "ERP y gestión"),
    ("automatizacion", "Automatización"),
    ("energia", "Energía"),
    ("ciberseguridad", "Ciberseguridad"),
    ("exportacion", "Exportación"),
    ("formacion", "Formación"),
    ("id_producto", "I+D de producto"),
    ("contratacion", "Contratación"),
]


def upgrade() -> None:
    op.add_column("empresa", sa.Column("facturacion_tramo", sa.String(length=12), nullable=True))
    op.add_column("empresa", sa.Column("empleados", sa.Integer(), nullable=True))
    op.add_column("empresa", sa.Column("provincia", sa.String(length=60), nullable=True))
    op.add_column("empresa", sa.Column("municipio", sa.String(length=100), nullable=True))
    op.add_column("empresa", sa.Column("proyecto_previsto", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_empresa_facturacion_tramo", "empresa", f"facturacion_tramo IN {TRAMOS_FACTURACION}"
    )
    op.create_check_constraint(
        "ck_empresa_empleados", "empresa", "empleados IS NULL OR empleados BETWEEN 0 AND 1000000"
    )
    op.create_check_constraint(
        "ck_empresa_proyecto_previsto", "empresa", "proyecto_previsto IS NULL OR length(proyecto_previsto) <= 2000"
    )

    interes = op.create_table(
        "interes",
        sa.Column("codigo", sa.String(length=30), nullable=False),
        sa.Column("nombre", sa.String(length=100), nullable=False),
        sa.Column("orden", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("codigo"),
    )
    op.bulk_insert(
        interes, [{"codigo": c, "nombre": n, "orden": i} for i, (c, n) in enumerate(INTERESES, start=1)]
    )

    op.create_table(
        "empresa_interes",
        sa.Column("empresa_id", sa.BigInteger(), nullable=False),
        sa.Column("interes_codigo", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("empresa_id", "interes_codigo"),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresa.id"], name="fk_empresa_interes_empresa", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["interes_codigo"], ["interes.codigo"], name="fk_empresa_interes_interes"),
    )
    op.create_foreign_key("fk_empresa_interes_created_by", "empresa_interes", "usuario", ["created_by"], ["id"])
    op.create_foreign_key("fk_empresa_interes_updated_by", "empresa_interes", "usuario", ["updated_by"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_empresa_interes_updated_by", "empresa_interes", type_="foreignkey")
    op.drop_constraint("fk_empresa_interes_created_by", "empresa_interes", type_="foreignkey")
    op.drop_table("empresa_interes")
    op.drop_table("interes")
    op.drop_constraint("ck_empresa_proyecto_previsto", "empresa", type_="check")
    op.drop_constraint("ck_empresa_empleados", "empresa", type_="check")
    op.drop_constraint("ck_empresa_facturacion_tramo", "empresa", type_="check")
    op.drop_column("empresa", "proyecto_previsto")
    op.drop_column("empresa", "municipio")
    op.drop_column("empresa", "provincia")
    op.drop_column("empresa", "empleados")
    op.drop_column("empresa", "facturacion_tramo")
