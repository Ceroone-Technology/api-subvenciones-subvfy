"""Schema of `convocatoria.cnae_secciones` (PR B2): live catalog checks and up/down on a scratch database."""

from sqlalchemy import text

from app.database import AsyncSessionLocal
from tests.migraciones import base_temporal

REVISION_ANTERIOR = "a7d2e9c41b36"


async def test_column_is_a_not_null_char1_array_defaulting_to_empty() -> None:
    async with AsyncSessionLocal() as db:
        col = (
            await db.execute(
                text(
                    "SELECT udt_name, is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'convocatoria' AND column_name = 'cnae_secciones'"
                )
            )
        ).one()
        indice = (
            await db.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_convocatoria_cnae_secciones'"))
        ).scalar_one()
    assert (col[0], col[1]) == ("_bpchar", "NO")
    assert "'{}'" in col[2]
    assert "USING gin" in indice and "(cnae_secciones)" in indice


async def test_migration_up_down_on_scratch_database() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", REVISION_ANTERIOR)
        async with base.motor.begin() as c:
            await c.execute(text("INSERT INTO convocatoria (codigo_bdns, titulo) VALUES ('X-1', 'Vieja')"))
        base.alembic("upgrade", "head")
        async with base.motor.connect() as c:
            fila = (await c.execute(text("SELECT cnae_secciones FROM convocatoria WHERE codigo_bdns = 'X-1'"))).one()
        assert fila[0] == []  # existing rows start unresolved; the next detail sync (or the CLI) fills them

        base.alembic("downgrade", REVISION_ANTERIOR)
        async with base.motor.connect() as c:
            cols = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'convocatoria' AND column_name = 'cnae_secciones'"
                    )
                )
            ).scalar_one()
            sin_indice = text("SELECT to_regclass('ix_convocatoria_cnae_secciones') IS NULL")
            indice = (await c.execute(sin_indice)).scalar_one()
            titulo = (await c.execute(text("SELECT titulo FROM convocatoria WHERE codigo_bdns = 'X-1'"))).scalar_one()
        assert (cols, indice, titulo) == (0, True, "Vieja")
        base.alembic("upgrade", "head")  # reversible
