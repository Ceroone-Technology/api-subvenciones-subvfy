"""CNAE-2009 -> CNAE-2025 migration (PO decision 06/11): data remap up and down on a scratch database."""

from sqlalchemy import text

from tests.migraciones import base_temporal

REVISION_ANTERIOR = "b3e8f5a2d917"  # CNAE-2009 catalog, 21 sections


async def _activ(c, nif: str) -> tuple[str | None, str | None]:
    fila = (
        await c.execute(text("SELECT actividad_codigo, actividad_otra FROM empresa WHERE nif = :n"), {"n": nif})
    ).one()
    return fila[0], fila[1]


async def _secciones(c, codigo: str) -> list[str]:
    q = text("SELECT cnae_secciones FROM convocatoria WHERE codigo_bdns = :c")
    return list((await c.execute(q, {"c": codigo})).scalar_one())


async def test_empresa_y_convocatorias_se_remapean_a_cnae_2025_y_vuelven() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", REVISION_ANTERIOR)
        async with base.motor.begin() as c:
            for nif, codigo, otra in [
                ("E-A", "A", None), ("E-I", "I", None), ("E-J", "J", None), ("E-K", "K", None),
                ("E-O", "O", None), ("E-U", "U", None), ("E-OTRA", "OTRA", "Apicultura"), ("E-NULA", None, "hint"),
            ]:  # fmt: skip
                await c.execute(
                    text(
                        "INSERT INTO empresa (razon_social, nif, actividad_codigo, actividad_otra) "
                        "VALUES (:n, :n, :c, :o)"
                    ),
                    {"n": nif, "c": codigo, "o": otra},
                )
            for codigo, secciones in [
                ("C-1", ["C", "O"]), ("C-2", ["K", "U"]), ("C-3", ["C", "J"]), ("C-4", ["J"]), ("C-5", []),
            ]:  # fmt: skip
                await c.execute(
                    text(
                        "INSERT INTO convocatoria (codigo_bdns, titulo, cnae_secciones) "
                        "VALUES (:c, 't', CAST(:s AS char(1)[]))"
                    ),
                    {"c": codigo, "s": secciones},
                )

        base.alembic("upgrade", "head")
        async with base.motor.connect() as c:
            assert await _activ(c, "E-A") == ("A", None)  # A-I keep their letter
            assert await _activ(c, "E-I") == ("I", None)
            assert await _activ(c, "E-J") == (None, None)  # J split in two: ambiguous -> pending
            assert await _activ(c, "E-K") == ("L", None)  # financial moved K -> L
            assert await _activ(c, "E-O") == ("P", None)  # public administration O -> P
            assert await _activ(c, "E-U") == ("V", None)  # extraterritorial U -> V
            assert await _activ(c, "E-OTRA") == ("OTRA", "Apicultura")
            assert await _activ(c, "E-NULA") == (None, "hint")
            assert await _secciones(c, "C-1") == ["C", "P"]
            assert await _secciones(c, "C-2") == ["L", "V"]
            assert (
                await _secciones(c, "C-3") == []
            )  # carried a J: cannot be resolved offline, `rellenar-cnae` refills it
            assert await _secciones(c, "C-4") == []
            assert await _secciones(c, "C-5") == []
            catalogo = (await c.execute(text("SELECT count(*), max(version_cnae) FROM sector_actividad"))).one()
            assert tuple(catalogo) == (23, "CNAE-2025")

        # Downgrade maps back (J and K both fold into the old J) and restores the 2009 catalog.
        async with base.motor.begin() as c:
            await c.execute(text("UPDATE empresa SET actividad_codigo = 'K' WHERE nif = 'E-NULA'"))  # a 2025 K company
            await c.execute(
                text("UPDATE convocatoria SET cnae_secciones = ARRAY['J','K','P']::char(1)[] WHERE codigo_bdns = 'C-5'")
            )
        base.alembic("downgrade", REVISION_ANTERIOR)
        async with base.motor.connect() as c:
            assert await _activ(c, "E-K") == ("K", None)  # 2025 L -> 2009 K
            assert await _activ(c, "E-O") == ("O", None)
            assert await _activ(c, "E-U") == ("U", None)
            assert await _activ(c, "E-NULA") == ("J", "hint")  # 2025 K -> 2009 J
            assert await _secciones(c, "C-1") == ["C", "O"]
            assert await _secciones(c, "C-5") == ["J", "O"]  # J and K merge into J, no duplicates
            catalogo = (await c.execute(text("SELECT count(*), max(version_cnae) FROM sector_actividad"))).one()
            assert tuple(catalogo) == (22, "CNAE-2009")
        base.alembic("upgrade", "head")  # reversible
