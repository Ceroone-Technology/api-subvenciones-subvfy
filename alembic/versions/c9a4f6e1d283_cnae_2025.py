"""company activity and grant sections: CNAE-2009 -> CNAE-2025

PO decision 06/11: the BDNS `/actividades` now returns CNAE-2025 (22 sections, A-V). The old
J (information and communications) is split into J (58-60) and K (61-63), so every later
section shifts one letter: K->L ... U->V; A-I are unchanged. Source of the list: the official
CNAE-2025 structure (INE), checked against the BDNS response recorded in
`tests/fixtures/bdns/actividades_cnae2025_real.json`.

Written by hand. There is no CHECK on the letters (only `ck_empresa_actividad_otra`, which does
not depend on them); the allowed set is enforced by `fk_empresa_actividad` -> `sector_actividad`,
so the FK is dropped while the catalog is swapped and recreated afterwards.

Upgrade: `empresa.actividad_codigo` shifts K..U by one letter; old J is ambiguous, so those
companies go back to pending (NULL) and must pick again. `convocatoria.cnae_secciones` shifts the
same way; a row that carried a J cannot be resolved offline (the stored `sectores` are descriptions
without codes), so it is reset to `{}` and `python -m app.cli rellenar-cnae` refills it.

Downgrade: letters L..V shift back by one and the new K folds into the old J (J and K merge, no
duplicates in the arrays); the 2009 catalog is restored. Nothing is lost beyond the J/K split.

Revision ID: c9a4f6e1d283
Revises: b3e8f5a2d917
Create Date: 2026-11-06 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9a4f6e1d283"
down_revision: Union[str, None] = "b3e8f5a2d917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (codigo, descripcion, division_desde, division_hasta); `orden` is the position.
SECCIONES_2025 = [
    ("A", "Agricultura, ganadería, silvicultura y pesca", 1, 3),
    ("B", "Industrias extractivas", 5, 9),
    ("C", "Industria manufacturera", 10, 33),
    ("D", "Suministro de energía eléctrica, gas, vapor y aire acondicionado", 35, 35),
    ("E", "Suministro de agua, actividades de saneamiento, gestión de residuos y descontaminación", 36, 39),
    ("F", "Construcción", 41, 43),
    ("G", "Comercio al por mayor y al por menor", 45, 47),
    ("H", "Transporte y almacenamiento", 49, 53),
    ("I", "Hostelería", 55, 56),
    ("J", "Actividades de edición, radiodifusión y producción y distribución de contenidos", 58, 60),
    (
        "K",
        "Telecomunicaciones, programación informática, consultoría, infraestructura informática "
        "y otros servicios de información",
        61,
        63,
    ),
    ("L", "Actividades financieras y de seguros", 64, 66),
    ("M", "Actividades inmobiliarias", 68, 68),
    ("N", "Actividades profesionales, científicas y técnicas", 69, 75),
    ("O", "Actividades administrativas y servicios auxiliares", 77, 82),
    ("P", "Administración pública y defensa; seguridad social obligatoria", 84, 84),
    ("Q", "Educación", 85, 85),
    ("R", "Actividades sanitarias y de servicios sociales", 86, 88),
    ("S", "Actividades artísticas, deportivas y de entretenimiento", 90, 93),
    ("T", "Otros servicios", 94, 96),
    (
        "U",
        "Actividades de los hogares como empleadores y como productores de bienes y servicios "
        "no diferenciados para uso propio",
        97,
        98,
    ),
    ("V", "Organismos extraterritoriales", 99, 99),
    ("OTRA", "Otra actividad (especificar)", None, None),
]

SECCIONES_2009 = [
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


def _cambiar_catalogo(secciones: list, version: str) -> None:
    op.execute("DELETE FROM sector_actividad")
    catalogo = sa.table(
        "sector_actividad",
        sa.column("codigo", sa.String),
        sa.column("descripcion", sa.String),
        sa.column("orden", sa.SmallInteger),
        sa.column("division_desde", sa.SmallInteger),
        sa.column("division_hasta", sa.SmallInteger),
        sa.column("version_cnae", sa.String),
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
                "version_cnae": version,
            }
            for orden, (codigo, descripcion, desde, hasta) in enumerate(secciones, start=1)
        ],
    )


def upgrade() -> None:
    op.drop_constraint("fk_empresa_actividad", "empresa", type_="foreignkey")
    # K..U move one letter up (`chr(ascii + 1)`), A-I stay, and the ambiguous J goes back to pending.
    # Regex, not BETWEEN: 'OTRA' sorts between 'J' and 'U' as text.
    op.execute(
        "UPDATE empresa SET actividad_codigo = CASE WHEN actividad_codigo = 'J' THEN NULL "
        "ELSE chr(ascii(actividad_codigo) + 1) END "
        "WHERE actividad_codigo ~ '^[J-U]$'"
    )
    # A row that carried a J cannot be resolved offline: reset it. The others shift (order is kept).
    op.execute(
        "UPDATE convocatoria SET cnae_secciones = CASE WHEN 'J' = ANY(cnae_secciones) THEN '{}'::char(1)[] "
        "ELSE ARRAY(SELECT (CASE WHEN ascii(s) >= 75 THEN chr(ascii(s) + 1) ELSE s::text END)::char(1) "
        "FROM unnest(cnae_secciones) WITH ORDINALITY AS t(s, n) ORDER BY n) END "
        "WHERE cnae_secciones <> '{}'::char(1)[]"
    )
    _cambiar_catalogo(SECCIONES_2025, "CNAE-2025")
    op.create_foreign_key("fk_empresa_actividad", "empresa", "sector_actividad", ["actividad_codigo"], ["codigo"])


def downgrade() -> None:
    op.drop_constraint("fk_empresa_actividad", "empresa", type_="foreignkey")
    # The new J and K both fold into the old J; L..V move one letter down.
    op.execute(
        "UPDATE empresa SET actividad_codigo = CASE WHEN actividad_codigo IN ('J', 'K') THEN 'J' "
        "ELSE chr(ascii(actividad_codigo) - 1) END "
        "WHERE actividad_codigo ~ '^[J-V]$'"
    )
    op.execute(
        "UPDATE convocatoria SET cnae_secciones = ARRAY(SELECT DISTINCT m FROM ("
        "SELECT (CASE WHEN s IN ('J', 'K') THEN 'J' WHEN ascii(s) >= 76 THEN chr(ascii(s) - 1) ELSE s::text END)::char(1) AS m "
        "FROM unnest(cnae_secciones) AS s) x ORDER BY m) "
        "WHERE cnae_secciones <> '{}'::char(1)[]"
    )
    _cambiar_catalogo(SECCIONES_2009, "CNAE-2009")
    op.create_foreign_key("fk_empresa_actividad", "empresa", "sector_actividad", ["actividad_codigo"], ["codigo"])
