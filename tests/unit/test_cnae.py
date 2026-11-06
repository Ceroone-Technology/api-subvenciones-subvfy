"""Pure CNAE helpers (PR B2): division -> section and BDNS `sectores` -> section letters."""

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.services.cnae import RANGOS_DIVISION, seccion_de, secciones_de


@pytest.mark.parametrize(
    ("codigo", "esperada"),
    [
        ("84.1", "O"),  # the BDNS fixture code
        ("01", "A"),
        ("1.1", "A"),
        ("03", "A"),
        ("10", "C"),
        ("33.20", "C"),
        ("35", "D"),
        ("62.01", "J"),
        ("86", "Q"),
        ("99", "U"),
        ("841", "O"),  # no separator: first two digits are the division
        (" 47.11 ", "G"),
        ("C", "C"),  # already a section letter
        ("u", "U"),
    ],
)
def test_seccion_de_codes_that_map(codigo: str, esperada: str) -> None:
    assert seccion_de(codigo) == esperada


@pytest.mark.parametrize("codigo", ["00", "04", "34", "40", "OTRA", "V", "abc", "", "  ", None])
def test_seccion_de_codes_that_do_not_map(codigo: str | None) -> None:
    assert seccion_de(codigo) is None


def test_secciones_de_returns_sorted_unique_letters() -> None:
    sectores = [{"codigo": "84.1"}, {"codigo": "62.01"}, {"codigo": "84.2", "descripcion": "x"}, {"codigo": "zz"}]
    assert secciones_de(sectores) == ["J", "O"]


BASURA = [None, "texto", 5, {}, [None, 3, "84", {"descripcion": "sin codigo"}, {"codigo": 84}]]


@pytest.mark.parametrize("basura", BASURA)
def test_secciones_de_ignores_anything_that_is_not_a_coded_sector(basura: object) -> None:
    assert secciones_de(basura) == []


async def test_in_code_ranges_mirror_the_seeded_catalog() -> None:
    async with AsyncSessionLocal() as db:
        filas = (
            await db.execute(
                text("SELECT codigo, division_desde, division_hasta FROM sector_actividad WHERE codigo <> 'OTRA'")
            )
        ).all()
    assert len(filas) == 21
    assert {f[0]: (f[1], f[2]) for f in filas} == RANGOS_DIVISION
