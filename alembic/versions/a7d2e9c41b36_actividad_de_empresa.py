"""company activity (CNAE sections)

Change `empresa-actividad-ia`, PR B1: static catalog `sector_actividad` (21 CNAE-2009
sections with their division ranges + `OTRA`) and `empresa.actividad_codigo` /
`actividad_otra`.

Written by hand: Alembic does not compare CHECKs, so `ck_empresa_actividad_otra` is
created here and dropped in the downgrade. Existing companies stay pending
(`actividad_codigo` NULL); their legacy free-text `sector` is copied into
`actividad_otra` as a display hint only (the CHECK only bites when the code is `OTRA`).
The downgrade drops the new columns and the catalog; `sector` is never touched, so
nothing is lost.

Revision ID: a7d2e9c41b36
Revises: f1b8d3a67c24
Create Date: 2026-11-06 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7d2e9c41b36"
down_revision: Union[str, None] = "f1b8d3a67c24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VERSION_CNAE = "CNAE-2009"

# (codigo, descripcion, division_desde, division_hasta); `orden` is the position.
SECCIONES = [
    ("A", "Agricultura, ganadería, silvicultura y pesca", 1, 3),
    ("B", "Industrias extractivas", 5, 9),
    ("C", "Industria manufacturera", 10, 33),
    ("D", "Suministro de energía eléctrica, gas, vapor y aire acondicionado", 35, 35),
    ("E", "Suministro de agua, actividades de saneamiento, gestión de residuos y descontaminación", 36, 39),
    ("F", "Construcción", 41, 43),
    ("G", "Comercio al por mayor y al por menor; reparación de vehículos de motor y motocicletas", 45, 47),
    ("H", "Transporte y almacenamiento", 49, 53),
    ("I", "Hostelería", 55, 56),
    ("J", "Información y comunicaciones", 58, 63),
    ("K", "Actividades financieras y de seguros", 64, 66),
    ("L", "Actividades inmobiliarias", 68, 68),
    ("M", "Actividades profesionales, científicas y técnicas", 69, 75),
    ("N", "Actividades administrativas y servicios auxiliares", 77, 82),
    ("O", "Administración pública y defensa; seguridad social obligatoria", 84, 84),
    ("P", "Educación", 85, 85),
    ("Q", "Actividades sanitarias y de servicios sociales", 86, 88),
    ("R", "Actividades artísticas, recreativas y de entretenimiento", 90, 93),
    ("S", "Otros servicios", 94, 96),
    (
        "T",
        "Actividades de los hogares como empleadores de personal doméstico; "
        "actividades de los hogares como productores de bienes y servicios para uso propio",
        97,
        98,
    ),
    ("U", "Actividades de organizaciones y organismos extraterritoriales", 99, 99),
    ("OTRA", "Otra actividad (especificar)", None, None),
]


def upgrade() -> None:
    catalogo = op.create_table(
        "sector_actividad",
        sa.Column("codigo", sa.String(length=4), nullable=False),
        sa.Column("descripcion", sa.String(length=200), nullable=False),
        sa.Column("orden", sa.SmallInteger(), nullable=False),
        sa.Column("division_desde", sa.SmallInteger(), nullable=True),
        sa.Column("division_hasta", sa.SmallInteger(), nullable=True),
        sa.Column("version_cnae", sa.String(length=10), nullable=False),
        sa.PrimaryKeyConstraint("codigo"),
    )
    op.bulk_insert(
        catalogo,
        [
            {
                "codigo": codigo,
                "descripcion": descripcion,
                "orden": orden,
                "division_desde": desde,
                "division_hasta": hasta,
                "version_cnae": VERSION_CNAE,
            }
            for orden, (codigo, descripcion, desde, hasta) in enumerate(SECCIONES, start=1)
        ],
    )

    op.add_column("empresa", sa.Column("actividad_codigo", sa.String(length=4), nullable=True))
    op.add_column("empresa", sa.Column("actividad_otra", sa.String(length=200), nullable=True))
    op.create_foreign_key("fk_empresa_actividad", "empresa", "sector_actividad", ["actividad_codigo"], ["codigo"])
    op.create_check_constraint(
        "ck_empresa_actividad_otra",
        "empresa",
        "actividad_codigo <> 'OTRA' OR length(trim(actividad_otra)) > 0",
    )
    # Legacy free text kept as a hint for the pending companies (never as the declared activity).
    op.execute("UPDATE empresa SET actividad_otra = left(trim(sector), 200) WHERE trim(coalesce(sector, '')) <> ''")


def downgrade() -> None:
    op.drop_constraint("ck_empresa_actividad_otra", "empresa", type_="check")
    op.drop_constraint("fk_empresa_actividad", "empresa", type_="foreignkey")
    op.drop_column("empresa", "actividad_otra")
    op.drop_column("empresa", "actividad_codigo")
    op.drop_table("sector_actividad")
