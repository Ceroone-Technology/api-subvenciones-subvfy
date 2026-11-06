"""Extended company profile (N2): fields, interests, completeness, `/empresas/me` and the interests catalog."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.models.interes import INTERESES
from tests.conftest import Sesion
from tests.migraciones import base_temporal


async def mi_empresa(cliente: AsyncClient) -> dict:
    r = await cliente.get("/empresas/me")
    assert r.status_code == 200, r.text
    return r.json()


# --- Catalog ----------------------------------------------------------------------


async def test_catalogo_de_intereses(client_usuario: AsyncClient, client: AsyncClient) -> None:
    r = await client_usuario.get("/catalogos/intereses")
    assert r.status_code == 200
    assert [(i["codigo"], i["nombre"]) for i in r.json()] == list(INTERESES)
    assert [i["orden"] for i in r.json()] == list(range(1, 9))
    assert (await client.get("/catalogos/intereses")).status_code == 401


async def test_la_base_tiene_los_mismos_intereses_que_el_codigo() -> None:
    async with AsyncSessionLocal() as db:
        filas = (await db.execute(text("SELECT codigo, nombre FROM interes ORDER BY orden"))).all()
    assert [tuple(f) for f in filas] == list(INTERESES)


# --- /empresas/me --------------------------------------------------------------------


async def test_me_de_una_empresa_sin_datos_esta_a_cero(client_usuario: AsyncClient, empresa: dict) -> None:
    cuerpo = await mi_empresa(client_usuario)
    assert cuerpo["id"] == empresa["id"]
    # the fixture company has declared nothing yet (its activity is pending)
    assert cuerpo["perfil_completo_pct"] == 0
    pendientes = [p["codigo"] for p in cuerpo["perfil_pendiente"]]
    assert pendientes == ["actividad", "tamano_ubicacion", "intereses", "facturacion", "proyecto"]
    assert cuerpo["perfil_pendiente"][1]["etiqueta"] == "Tamaño y ubicación"
    assert cuerpo["intereses"] == []
    opcionales = ("facturacion_tramo", "empleados", "provincia", "municipio", "proyecto_previsto")
    assert all(cuerpo[c] is None for c in opcionales)


async def test_me_requiere_autenticacion(client: AsyncClient) -> None:
    assert (await client.get("/empresas/me")).status_code == 401


async def test_me_devuelve_la_empresa_de_cada_token(
    client_gestor: AsyncClient, client_admin: AsyncClient, gestor: Sesion, admin: Sesion
) -> None:
    assert (await mi_empresa(client_gestor))["id"] == gestor.empresa_id
    assert (await mi_empresa(client_admin))["id"] == admin.empresa_id != gestor.empresa_id


# --- Edit -------------------------------------------------------------------------------


PERFIL = {
    "tamano": "pequena",
    "provincia": "Valencia",
    "municipio": "Paterna",
    "empleados": 34,
    "facturacion_tramo": "2m_10m",
    "proyecto_previsto": "Nueva línea de mecanizado en 2027",
    "intereses": ["energia", "erp", "automatizacion"],
}


async def test_el_gestor_completa_el_perfil(client_gestor: AsyncClient, gestor: Sesion) -> None:
    r = await client_gestor.patch(f"/empresas/{gestor.empresa_id}", json={**PERFIL, "actividad_codigo": "C"})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["perfil_completo_pct"] == 100 and cuerpo["perfil_pendiente"] == []
    assert cuerpo["intereses"] == ["erp", "automatizacion", "energia"]  # catalog order, not request order
    assert (cuerpo["provincia"], cuerpo["municipio"], cuerpo["empleados"]) == ("Valencia", "Paterna", 34)
    assert await mi_empresa(client_gestor) == cuerpo


async def test_los_intereses_se_reemplazan_y_se_vacian(client_gestor: AsyncClient, gestor: Sesion) -> None:
    url = f"/empresas/{gestor.empresa_id}"
    await client_gestor.patch(url, json={"intereses": ["erp", "energia"]})
    assert (await client_gestor.patch(url, json={"intereses": ["formacion"]})).json()["intereses"] == ["formacion"]
    # not sending them leaves them alone
    assert (await client_gestor.patch(url, json={"municipio": "X"})).json()["intereses"] == ["formacion"]
    assert (await client_gestor.patch(url, json={"intereses": []})).json()["intereses"] == []


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"intereses": ["inventado"]},
        {"intereses": ["erp", "erp"]},
        {"intereses": None},
        {"facturacion_tramo": "mucho"},
        {"empleados": -1},
        {"empleados": 1_000_001},
        {"proyecto_previsto": "x" * 2001},
        {"provincia": "x" * 61},
    ],
)
async def test_valores_invalidos_dan_422(client_gestor: AsyncClient, gestor: Sesion, cuerpo: dict) -> None:
    assert (await client_gestor.patch(f"/empresas/{gestor.empresa_id}", json=cuerpo)).status_code == 422


async def test_un_texto_en_blanco_se_guarda_como_vacio(client_gestor: AsyncClient, gestor: Sesion) -> None:
    url = f"/empresas/{gestor.empresa_id}"
    await client_gestor.patch(url, json={"provincia": "Soria", "proyecto_previsto": "algo"})
    cuerpo = (await client_gestor.patch(url, json={"provincia": "   ", "proyecto_previsto": ""})).json()
    assert cuerpo["provincia"] is None and cuerpo["proyecto_previsto"] is None


async def test_se_puede_borrar_un_dato_opcional_con_null(client_gestor: AsyncClient, gestor: Sesion) -> None:
    url = f"/empresas/{gestor.empresa_id}"
    await client_gestor.patch(url, json={"facturacion_tramo": "mas_50m", "empleados": 5})
    cuerpo = (await client_gestor.patch(url, json={"facturacion_tramo": None, "empleados": None})).json()
    assert cuerpo["facturacion_tramo"] is None and cuerpo["empleados"] is None


async def test_el_usuario_no_edita_el_perfil(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    r = await client_usuario.patch(f"/empresas/{usuario_raso.empresa_id}", json={"municipio": "X"})
    assert r.status_code == 403


async def test_un_gestor_no_toca_los_intereses_de_otra_empresa(client_gestor: AsyncClient, otra_empresa: dict) -> None:
    r = await client_gestor.patch(f"/empresas/{otra_empresa['id']}", json={"intereses": ["erp"]})
    assert r.status_code == 404
    assert (await client_gestor.get(f"/empresas/{otra_empresa['id']}")).status_code == 404


async def test_crear_empresa_con_perfil(client_admin: AsyncClient, nif_unico: str) -> None:
    r = await client_admin.post(
        "/empresas",
        json={"razon_social": "Perfil SL", "nif": nif_unico, "actividad_codigo": "C", **PERFIL},
    )
    assert r.status_code == 201, r.text
    assert r.json()["perfil_completo_pct"] == 100 and r.json()["intereses"] == ["erp", "automatizacion", "energia"]


async def test_el_listado_trae_el_perfil_de_cada_empresa(client_admin: AsyncClient, nif_unico: str) -> None:
    alta = {"razon_social": "Listada SL", "nif": nif_unico, "actividad_codigo": "C", "intereses": ["erp"]}
    creada = (await client_admin.post("/empresas", json=alta)).json()
    items = (await client_admin.get("/empresas", params={"q": nif_unico})).json()["items"]
    assert [i["id"] for i in items] == [creada["id"]]
    assert items[0]["intereses"] == ["erp"] and items[0]["perfil_completo_pct"] == 20


# --- Schema ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE empresa SET facturacion_tramo = 'x' WHERE id = :i",
        "UPDATE empresa SET empleados = -5 WHERE id = :i",
        "UPDATE empresa SET proyecto_previsto = repeat('x', 2001) WHERE id = :i",
    ],
)
async def test_los_check_rechazan_valores_fuera_de_rango(empresa: dict, sql: str) -> None:
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError):
            await db.execute(text(sql), {"i": empresa["id"]})


async def test_un_interes_no_se_repite_ni_apunta_a_un_codigo_inexistente(empresa: dict) -> None:
    insertar = text("INSERT INTO empresa_interes (empresa_id, interes_codigo) VALUES (:e, :c)")
    async with AsyncSessionLocal() as db:
        await db.execute(insertar, {"e": empresa["id"], "c": "erp"})
        await db.commit()
    for codigo in ("erp", "inventado"):
        async with AsyncSessionLocal() as db:
            with pytest.raises(IntegrityError):
                await db.execute(insertar, {"e": empresa["id"], "c": codigo})


async def test_migracion_sube_y_baja() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", "d2b7a91c5e48")
        async with base.motor.begin() as c:
            await c.execute(text("INSERT INTO empresa (razon_social, nif, actividad_codigo) VALUES ('A', 'N-1', 'C')"))
        base.alembic("upgrade", "head")
        async with base.motor.begin() as c:
            q = text("SELECT facturacion_tramo, empleados, provincia FROM empresa WHERE nif = 'N-1'")
            fila = (await c.execute(q)).one()
            assert tuple(fila) == (None, None, None)
            assert (await c.execute(text("SELECT count(*) FROM interes"))).scalar() == 8
            insertar = (
                "INSERT INTO empresa_interes (empresa_id, interes_codigo) "
                "SELECT id, 'erp' FROM empresa WHERE nif = 'N-1'"
            )
            await c.execute(text(insertar))
            q = text("SELECT conname FROM pg_constraint WHERE conrelid = 'empresa_interes'::regclass")
            nombres = {f[0] for f in await c.execute(q)}
            assert {"fk_empresa_interes_created_by", "fk_empresa_interes_updated_by"} <= nombres
        base.alembic("downgrade", "d2b7a91c5e48")
        async with base.motor.connect() as c:
            q = text("SELECT column_name FROM information_schema.columns WHERE table_name = 'empresa'")
            columnas = {f[0] for f in await c.execute(q)}
            assert not {"facturacion_tramo", "empleados", "provincia", "municipio", "proyecto_previsto"} & columnas
            assert (await c.execute(text("SELECT to_regclass('interes')"))).scalar() is None
            assert (await c.execute(text("SELECT count(*) FROM empresa WHERE nif = 'N-1'"))).scalar() == 1
        base.alembic("upgrade", "head")


async def test_autonomo_es_un_tamano_valido(client_gestor: AsyncClient, gestor: Sesion) -> None:
    r = await client_gestor.patch(f"/empresas/{gestor.empresa_id}", json={"tamano": "autonomo"})
    assert r.status_code == 200 and r.json()["tamano"] == "autonomo"
    assert (await client_gestor.patch(f"/empresas/{gestor.empresa_id}", json={"tamano": "gigante"})).status_code == 422


async def test_migracion_autonomo_sube_y_baja_recolocando_filas() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", "f7a3c9d15e82")
        async with base.motor.begin() as c:
            await c.execute(text("INSERT INTO empresa (razon_social, nif, tamano) VALUES ('A', 'N-A', 'autonomo')"))
        base.alembic("downgrade", "e5c1d8a74b20")
        async with base.motor.connect() as c:
            assert (await c.execute(text("SELECT tamano FROM empresa WHERE nif = 'N-A'"))).scalar() == "micro"
        base.alembic("upgrade", "head")


async def test_dos_patch_simultaneos_de_intereses_no_dan_500(client_gestor: AsyncClient, gestor: Sesion) -> None:
    import asyncio

    url = f"/empresas/{gestor.empresa_id}"
    respuestas = await asyncio.gather(
        *(client_gestor.patch(url, json={"intereses": ["erp", "energia", "formacion"]}) for _ in range(8))
    )
    assert [r.status_code for r in respuestas] == [200] * 8
    assert (await mi_empresa(client_gestor))["intereses"] == ["erp", "energia", "formacion"]
