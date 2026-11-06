"""Own classification (N1) against the real database.

Rows are tagged with a unique title word (`marca`) and the recalculation only touches the `TST` test prefix, so real
grants are never read for assertions nor modified.
"""

import random
import string
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.models import Convocatoria, GestionExcepcion
from app.services.clasificacion import recalcular
from tests.conftest import Sesion, crear_convocatoria_en_bd
from tests.migraciones import base_temporal
from tests.test_sync_bdns import FalsaBdns, correr

HOY = date.today()


def marca() -> str:
    return "zz" + "".join(random.choices(string.ascii_lowercase, k=12))


async def sembrar(m: str, titulo: str, **extra: Any) -> str:
    return (await crear_convocatoria_en_bd(f"{titulo} {m}", **extra)).codigo_bdns


async def fila(codigo: str) -> Convocatoria:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Convocatoria).where(Convocatoria.codigo_bdns == codigo))).scalar_one()


async def recalcular_de_prueba():  # type: ignore[no-untyped-def]
    async with AsyncSessionLocal() as db:
        return await recalcular(db, prefijo="TST")


# --- Schema ---------------------------------------------------------------------


async def test_el_check_rechaza_un_tema_desconocido() -> None:
    conv = await crear_convocatoria_en_bd("Tema raro")
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError):
            await db.execute(text("UPDATE convocatoria SET tema = 'xx' WHERE id = :i"), {"i": conv.id})


async def test_una_fila_nueva_nace_como_otro_y_sin_gestion() -> None:
    conv = await crear_convocatoria_en_bd("Sin clasificar")
    assert (conv.tema, conv.gestiona_subvfy) == ("otro", False)


async def test_una_excepcion_por_convocatoria() -> None:
    conv = await crear_convocatoria_en_bd("Excepcion unica")
    async with AsyncSessionLocal() as db:
        db.add(GestionExcepcion(convocatoria_id=conv.id, gestiona=True))
        await db.commit()
    async with AsyncSessionLocal() as db:
        db.add(GestionExcepcion(convocatoria_id=conv.id, gestiona=False))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_migracion_sube_y_baja_sin_perder_filas() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", "c9a4f6e1d283")
        async with base.motor.begin() as c:
            await c.execute(text("INSERT INTO convocatoria (codigo_bdns, titulo) VALUES ('M-1', 'Digitalización')"))
        base.alembic("upgrade", "head")
        async with base.motor.connect() as c:
            fila_ = (await c.execute(text("SELECT tema, gestiona_subvfy FROM convocatoria"))).one()
            assert tuple(fila_) == ("otro", False)  # recalculated afterwards by the CLI
            q = text("SELECT conname FROM pg_constraint WHERE conrelid = 'gestion_excepcion'::regclass")
            nombres = {f[0] for f in await c.execute(q)}
            assert {"fk_gestion_excepcion_created_by", "fk_gestion_excepcion_updated_by"} <= nombres
            assert "uq_gestion_excepcion_convocatoria" in nombres
            ck = (await c.execute(text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_convocatoria_tema'"))).first()
            assert ck is not None
        base.alembic("downgrade", "c9a4f6e1d283")
        async with base.motor.connect() as c:
            columnas = {
                f[0]
                for f in await c.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = 'convocatoria'")
                )
            }
            assert not {"tema", "gestiona_subvfy"} & columnas
            assert (await c.execute(text("SELECT to_regclass('gestion_excepcion')"))).scalar() is None
            assert (await c.execute(text("SELECT count(*) FROM convocatoria"))).scalar() == 1
        base.alembic("upgrade", "head")  # reversible


# --- Recalculation ------------------------------------------------------------------


async def test_recalcular_clasifica_y_es_idempotente() -> None:
    m = marca()
    dig = await sembrar(m, "Ayudas a la digitalización")
    ene = await sembrar(m, "Eficiencia energética")
    otro = await sembrar(m, "Subvención a la asociación")
    primera = await recalcular_de_prueba()
    assert primera.total >= 3 and primera.cambiadas >= 2
    assert [(await fila(c)).tema for c in (dig, ene, otro)] == ["dig", "ene", "otro"]
    assert [(await fila(c)).gestiona_subvfy for c in (dig, ene, otro)] == [True, False, False]
    segunda = await recalcular_de_prueba()
    assert segunda.cambiadas == 0


async def test_recalcular_usa_los_objetivos_del_detalle_y_respeta_las_excepciones() -> None:
    m = marca()
    id_ = await sembrar(m, "Ayudas", detalle={"objetivos": ["AYUDAS A PROYECTOS DE INVESTIGACIÓN Y DESARROLLO"]})
    forzada = await sembrar(m, "Eficiencia energética")
    vetada = await sembrar(m, "Ayudas a la digitalización")
    async with AsyncSessionLocal() as db:
        ids = {c: i for c, i in (await db.execute(select(Convocatoria.codigo_bdns, Convocatoria.id))).all()}
        db.add_all(
            [
                GestionExcepcion(convocatoria_id=ids[forzada], gestiona=True),
                GestionExcepcion(convocatoria_id=ids[vetada], gestiona=False),
            ]
        )
        await db.commit()
    await recalcular_de_prueba()
    assert (await fila(id_)).tema == "id" and (await fila(id_)).gestiona_subvfy is True
    assert (await fila(forzada)).tema == "ene" and (await fila(forzada)).gestiona_subvfy is True
    assert (await fila(vetada)).tema == "dig" and (await fila(vetada)).gestiona_subvfy is False


# --- Search ---------------------------------------------------------------------------


async def items(client: AsyncClient, m: str, **params: Any) -> list[dict[str, Any]]:
    r = await client.get("/convocatorias", params={"q": m, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_el_resumen_trae_region_tema_y_gestion(client_usuario: AsyncClient) -> None:
    m = marca()
    codigo = await sembrar(m, "Ayudas a la digitalización", region_ids=[3, 7], tema="dig", gestiona_subvfy=True)
    [item] = await items(client_usuario, m)
    assert item["codigo_bdns"] == codigo
    assert (item["region_ids"], item["tema"], item["gestiona_subvfy"]) == ([3, 7], "dig", True)
    ficha = (await client_usuario.get(f"/convocatorias/{codigo}")).json()
    assert (ficha["region_ids"], ficha["tema"], ficha["gestiona_subvfy"]) == ([3, 7], "dig", True)


async def test_filtros_tema_y_gestiona(client_usuario: AsyncClient) -> None:
    m = marca()
    dig = await sembrar(m, "A", tema="dig", gestiona_subvfy=True)
    ene = await sembrar(m, "B", tema="ene")
    otro = await sembrar(m, "C")
    forzada = await sembrar(m, "D", tema="emp", gestiona_subvfy=True)

    def codigos(lista: list[dict[str, Any]]) -> set[str]:
        return {i["codigo_bdns"] for i in lista}

    assert codigos(await items(client_usuario, m, tema="dig")) == {dig}
    assert codigos(await items(client_usuario, m, tema=["dig", "ene"])) == {dig, ene}
    assert codigos(await items(client_usuario, m, gestiona="true")) == {dig, forzada}
    assert codigos(await items(client_usuario, m, gestiona="false")) == {ene, otro}
    assert codigos(await items(client_usuario, m, tema="emp", gestiona="true")) == {forzada}
    assert codigos(await items(client_usuario, m)) == {dig, ene, otro, forzada}
    r = await client_usuario.get("/convocatorias", params={"tema": "xx"})
    assert r.status_code == 422


async def test_orden_cierre_proximas_primero_luego_sin_fecha_y_cerradas_al_final(client_usuario: AsyncClient) -> None:
    m = marca()
    lejana = await sembrar(m, "lejana", fecha_fin_solicitud=HOY + timedelta(days=40))
    cercana = await sembrar(m, "cercana", fecha_fin_solicitud=HOY + timedelta(days=3))
    sin_fecha = await sembrar(m, "sin fecha")
    cerrada_vieja = await sembrar(m, "vieja", fecha_fin_solicitud=HOY - timedelta(days=60))
    cerrada_reciente = await sembrar(m, "reciente", fecha_fin_solicitud=HOY - timedelta(days=5))
    orden = [i["codigo_bdns"] for i in await items(client_usuario, m, orden="cierre")]
    assert orden == [cercana, lejana, sin_fecha, cerrada_reciente, cerrada_vieja]


async def test_catalogo_de_temas(client_usuario: AsyncClient, client: AsyncClient) -> None:
    r = await client_usuario.get("/catalogos/temas")
    assert r.status_code == 200
    assert [t["codigo"] for t in r.json()] == ["dig", "id", "ene", "int", "emp", "otro"]
    assert {t["codigo"] for t in r.json() if t["gestiona_subvfy"]} == {"dig", "id"}
    assert r.json()[0]["nombre"] == "Digitalización"
    assert (await client.get("/catalogos/temas")).status_code == 401


# --- Admin exceptions -----------------------------------------------------------------------


async def test_solo_el_admin_toca_las_excepciones(
    client: AsyncClient, client_gestor: AsyncClient, client_usuario: AsyncClient
) -> None:
    codigo = await sembrar(marca(), "X")
    url = f"/admin/gestion-excepciones/{codigo}"
    assert (await client.put(url, json={"gestiona": True})).status_code == 401
    for cliente in (client_gestor, client_usuario):
        assert (await cliente.put(url, json={"gestiona": True})).status_code == 403
        assert (await cliente.delete(url)).status_code == 403
        assert (await cliente.get("/admin/gestion-excepciones")).status_code == 403


async def test_el_admin_fuerza_y_quita_la_gestion(client_admin: AsyncClient, client_usuario: AsyncClient) -> None:
    m = marca()
    ene = await sembrar(m, "Eficiencia energética", tema="ene")
    dig = await sembrar(m, "Digitalización", tema="dig", gestiona_subvfy=True)

    r = await client_admin.put(f"/admin/gestion-excepciones/{ene}", json={"gestiona": True, "motivo": "Cliente VIP"})
    assert r.status_code == 200 and (r.json()["gestiona"], r.json()["motivo"]) == (True, "Cliente VIP")
    r = await client_admin.put(f"/admin/gestion-excepciones/{dig}", json={"gestiona": False})
    assert r.status_code == 200
    gestionadas = {i["codigo_bdns"] for i in await items(client_usuario, m, gestiona="true")}
    assert gestionadas == {ene}

    # PUT again updates (still one row), the list shows both
    r = await client_admin.put(f"/admin/gestion-excepciones/{ene}", json={"gestiona": True, "motivo": "Otro"})
    assert r.json()["motivo"] == "Otro"
    listado = (await client_admin.get("/admin/gestion-excepciones", params={"size": 100})).json()
    assert {e["codigo_bdns"] for e in listado["items"]} >= {ene, dig}
    assert listado["total"] == len(listado["items"]) or listado["total"] >= 2

    # Removing returns each grant to the rule
    assert (await client_admin.delete(f"/admin/gestion-excepciones/{ene}")).status_code == 204
    assert (await client_admin.delete(f"/admin/gestion-excepciones/{dig}")).status_code == 204
    assert {i["codigo_bdns"] for i in await items(client_usuario, m, gestiona="true")} == {dig}
    # nothing left to remove
    assert (await client_admin.delete(f"/admin/gestion-excepciones/{dig}")).status_code == 404


async def test_excepcion_de_convocatoria_desconocida_es_404(client_admin: AsyncClient) -> None:
    r = await client_admin.put("/admin/gestion-excepciones/NOEXISTE", json={"gestiona": True})
    assert r.status_code == 404
    assert (await client_admin.delete("/admin/gestion-excepciones/NOEXISTE")).status_code == 404


# --- Sync ----------------------------------------------------------------------------------


async def test_el_sync_clasifica_y_no_pisa_la_excepcion_del_admin(admin: Sesion) -> None:
    bdns = FalsaBdns()
    codigo = bdns.nueva()
    bdns.grants[codigo]["descripcion"] = "Ayudas a la digitalización de pymes"
    await correr(bdns, admin)
    primera = await fila(codigo)
    assert (primera.tema, primera.gestiona_subvfy) == ("dig", True)

    async with AsyncSessionLocal() as db:
        db.add(GestionExcepcion(convocatoria_id=primera.id, gestiona=False, motivo="no"))
        await db.execute(text("UPDATE convocatoria SET gestiona_subvfy = false WHERE id = :i"), {"i": primera.id})
        await db.commit()

    bdns.grants[codigo]["fechaFinSolicitud"] = "2026-12-15"  # content change -> the row is rewritten
    run = await correr(bdns, admin)
    assert run is not None and run.actualizadas == 1
    despues = await fila(codigo)
    assert despues.tema == "dig" and despues.gestiona_subvfy is False  # the override survived the refresh


async def test_la_excepcion_de_una_fila_no_contamina_a_las_demas_del_sync(admin: Sesion) -> None:
    """The override subquery must be correlated to the row being upserted, not to the whole table."""
    bdns = FalsaBdns()
    a, b, c = bdns.nueva(), bdns.nueva(), bdns.nueva()
    await correr(bdns, admin)
    filas = {codigo: await fila(codigo) for codigo in (a, b, c)}
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                GestionExcepcion(convocatoria_id=filas[a].id, gestiona=True),
                GestionExcepcion(convocatoria_id=filas[b].id, gestiona=True),
            ]
        )
        await db.commit()
    for codigo in (a, b, c):
        bdns.grants[codigo]["fechaFinSolicitud"] = "2026-12-20"  # rewrite all three rows
    run = await correr(bdns, admin)
    assert run is not None and run.estado == "ok" and run.actualizadas == 3
    assert [(await fila(x)).gestiona_subvfy for x in (a, b, c)] == [True, True, False]
