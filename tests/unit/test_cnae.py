"""Pure CNAE helpers (PR B2): division -> section and BDNS `sectores` -> section letters."""

import unicodedata

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.services.cnae import RANGOS_DIVISION, seccion_de, secciones_de
from tests.fixtures_bdns import cargar_fixture


@pytest.mark.parametrize(
    ("codigo", "esperada"),
    [
        ("84.1", "P"),  # the BDNS fixture code (CNAE-2025: public administration is P)
        ("01", "A"),
        ("1.1", "A"),
        ("03", "A"),
        ("10", "C"),
        ("33.20", "C"),
        ("35", "D"),
        ("62.01", "K"),  # CNAE-2025: IT moved to the new section K
        ("58", "J"),
        ("60.10", "J"),
        ("61", "K"),
        ("63", "K"),
        ("64", "L"),
        ("68", "M"),
        ("86", "R"),
        ("90", "S"),
        ("96", "T"),
        ("97", "U"),
        ("99", "V"),
        ("841", "P"),  # no separator: first two digits are the division
        (" 47.11 ", "G"),
        ("C", "C"),  # already a section letter
        ("v", "V"),
    ],
)
def test_seccion_de_codes_that_map(codigo: str, esperada: str) -> None:
    assert seccion_de(codigo) == esperada


@pytest.mark.parametrize("codigo", ["00", "04", "34", "40", "OTRA", "W", "abc", "", "  ", None])
def test_seccion_de_codes_that_do_not_map(codigo: str | None) -> None:
    assert seccion_de(codigo) is None


def test_secciones_de_returns_sorted_unique_letters() -> None:
    sectores = [{"codigo": "84.1"}, {"codigo": "62.01"}, {"codigo": "84.2", "descripcion": "x"}, {"codigo": "zz"}]
    assert secciones_de(sectores) == ["K", "P"]


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
    assert len(filas) == 22
    assert {f[0]: (f[1], f[2]) for f in filas} == RANGOS_DIVISION


async def test_the_catalog_matches_the_22_sections_the_bdns_lists_today() -> None:
    # `tests/fixtures/bdns/actividades_cnae2025_real.json`: GET /actividades recorded on 06/11/2026.
    grabado = cargar_fixture("actividades_cnae2025_real.json")
    assert len(grabado) == 22
    assert list(RANGOS_DIVISION) == list("ABCDEFGHIJKLMNOPQRSTUV")
    async with AsyncSessionLocal() as db:
        filas = (
            await db.execute(
                text("SELECT descripcion FROM sector_actividad WHERE codigo <> 'OTRA' ORDER BY orden")
            )
        ).scalars().all()
    # The BDNS drops accents in some names; the wording is the same.
    def sin_tildes(t: str) -> str:
        return unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode().casefold()

    assert [sin_tildes(f) for f in filas] == [sin_tildes(a["descripcion"]) for a in grabado]
