"""Schema of the company activity (PR B1, `empresa-actividad-ia`).

The first block inspects the live database. The last test runs the migration
up and down on a throwaway database (never on the shared one): it proves the
legacy-row handling and that the downgrade leaves the previous schema intact.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.models import Empresa, SectorActividad
from app.models.sector_actividad import CODIGOS_ACTIVIDAD
from tests.conftest import nif_de_prueba
from tests.migraciones import base_temporal

REVISION_ANTERIOR = "f1b8d3a67c24"

# CNAE-2025 sections with their division ranges (INE), the catalog at head.
SECCIONES = {
    "A": (1, 3), "B": (5, 9), "C": (10, 33), "D": (35, 35), "E": (36, 39), "F": (41, 43),
    "G": (45, 47), "H": (49, 53), "I": (55, 56), "J": (58, 60), "K": (61, 63), "L": (64, 66),
    "M": (68, 68), "N": (69, 75), "O": (77, 82), "P": (84, 84), "Q": (85, 85), "R": (86, 88),
    "S": (90, 93), "T": (94, 96), "U": (97, 98), "V": (99, 99),
}  # fmt: skip


async def _filas(sql: str, **params) -> list[dict]:
    async with AsyncSessionLocal() as db:
        return [dict(f) for f in (await db.execute(text(sql), params)).mappings().all()]


def test_codes_are_the_22_sections_plus_otra() -> None:
    assert CODIGOS_ACTIVIDAD == (*SECCIONES, "OTRA")
    assert len(CODIGOS_ACTIVIDAD) == 23


async def test_catalog_is_seeded_with_ranges() -> None:
    filas = await _filas(
        "SELECT codigo, orden, division_desde, division_hasta, version_cnae FROM sector_actividad ORDER BY orden"
    )
    assert [f["codigo"] for f in filas] == list(CODIGOS_ACTIVIDAD)
    assert [f["orden"] for f in filas] == list(range(1, 24))
    for f in filas[:-1]:
        assert (f["division_desde"], f["division_hasta"]) == SECCIONES[f["codigo"]]
        assert f["version_cnae"] == "CNAE-2025"
    otra = filas[-1]
    assert (otra["division_desde"], otra["division_hasta"]) == (None, None)


async def test_empresa_columns_fk_and_check_exist() -> None:
    cols = {
        f["column_name"]: f
        for f in await _filas(
            "SELECT column_name, is_nullable, character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'empresa' AND column_name LIKE 'actividad%'"
        )
    }
    assert set(cols) == {"actividad_codigo", "actividad_otra"}
    assert cols["actividad_codigo"]["is_nullable"] == "YES"
    assert cols["actividad_otra"]["character_maximum_length"] == 200
    filas = await _filas("SELECT conname, contype::text AS contype FROM pg_constraint")
    cons = {f["conname"]: f["contype"] for f in filas}
    assert cons["fk_empresa_actividad"] == "f"
    assert cons["ck_empresa_actividad_otra"] == "c"


async def test_check_rejects_otra_without_text() -> None:
    async with AsyncSessionLocal() as db:
        db.add(Empresa(razon_social="Check SL", nif=nif_de_prueba(), actividad_codigo="OTRA", actividad_otra="  "))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_check_accepts_otra_with_text_and_sections_without() -> None:
    async with AsyncSessionLocal() as db:
        db.add(Empresa(razon_social="Ok 1 SL", nif=nif_de_prueba(), actividad_codigo="OTRA", actividad_otra="Api"))
        db.add(Empresa(razon_social="Ok 2 SL", nif=nif_de_prueba(), actividad_codigo="C"))
        db.add(Empresa(razon_social="Legacy SL", nif=nif_de_prueba()))  # pending: NULL code
        await db.commit()


async def test_fk_rejects_unknown_section() -> None:
    async with AsyncSessionLocal() as db:
        db.add(Empresa(razon_social="Fk SL", nif=nif_de_prueba(), actividad_codigo="Z"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_catalog_model_reads_back() -> None:
    async with AsyncSessionLocal() as db:
        c = await db.get(SectorActividad, "C")
        assert c is not None
        assert (c.orden, c.division_desde, c.division_hasta) == (3, 10, 33)


# --- up / down on a throwaway database ----------------------------------------------------


async def test_migration_up_down_on_scratch_database() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", REVISION_ANTERIOR)
        async with base.motor.begin() as c:
            await c.execute(
                text("INSERT INTO empresa (razon_social, nif, sector) VALUES ('Vieja SL', 'X-1', 'Tecnologia')")
            )
        base.alembic("upgrade", "head")
        async with base.motor.connect() as c:
            vieja = (
                await c.execute(text("SELECT actividad_codigo, actividad_otra FROM empresa WHERE nif = 'X-1'"))
            ).one()
            n = (await c.execute(text("SELECT count(*) FROM sector_actividad"))).scalar_one()
        assert tuple(vieja) == (None, "Tecnologia")  # pending, sector kept as a hint
        assert n == 23

        base.alembic("downgrade", REVISION_ANTERIOR)
        async with base.motor.connect() as c:
            tablas = (await c.execute(text("SELECT to_regclass('sector_actividad') IS NULL"))).scalar_one()
            cols = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'empresa' AND column_name LIKE 'actividad%'"
                    )
                )
            ).scalar_one()
            sector = (await c.execute(text("SELECT sector FROM empresa WHERE nif = 'X-1'"))).scalar_one()
        assert tablas is True
        assert cols == 0
        assert sector == "Tecnologia"
        base.alembic("upgrade", "head")  # reversible: applies again cleanly
