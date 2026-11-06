"""Authenticated catalog routes (`bdns-local-sync`, PR5): BDNS node shape served from local data."""

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.api.routes.catalogos import router
from app.database import AsyncSessionLocal
from app.models import CatalogoBdns
from tests.conftest import crear_convocatoria_en_bd

BASE = 9_200_000
RUTAS = ["/catalogos/organos", "/catalogos/regiones", "/catalogos/beneficiarios"]


def fila(tipo: str, offset: int, descripcion: str, padre: int | None, nivel: str | None) -> CatalogoBdns:
    return CatalogoBdns(
        tipo=tipo, bdns_id=BASE + offset, padre_bdns_id=None if padre is None else BASE + padre,
        descripcion=f"TEST-{descripcion}", nivel_administracion=nivel, ruta_ids=[BASE + offset],
    )  # fmt: skip


async def sembrar_catalogo() -> None:
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                fila("organo", 1, "B MINISTERIO", None, "estado"),
                fila("organo", 3, "B MINISTERIO DG DOS", 1, "estado"),
                fila("organo", 2, "B MINISTERIO DG UNO", 1, "estado"),
                fila("organo", 4, "B CONSEJERIA", None, "ccaa"),
                fila("region", 10, "B ES99 - REGION", None, None),
                fila("region", 11, "B ES990 - PROVINCIA", 10, None),
            ]
        )
        await db.commit()


async def propios(client: AsyncClient, ruta: str, **params: Any) -> list[dict[str, Any]]:
    respuesta = await client.get(ruta, params=params)
    assert respuesta.status_code == 200
    return [n for n in respuesta.json() if n["descripcion"].startswith("TEST-B ")]


async def test_organos_en_forma_de_nodo_bdns_con_hijos_ordenados(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    raices = await propios(client_usuario, "/catalogos/organos")
    assert [r["id"] for r in raices] == [BASE + 4, BASE + 1]  # alfabético, ids BDNS intactos
    ministerio = raices[1]
    assert set(ministerio) == {"id", "descripcion", "children"}
    assert [h["id"] for h in ministerio["children"]] == [BASE + 3, BASE + 2]  # DG DOS antes que DG UNO
    assert ministerio["children"][1] == {"id": BASE + 2, "descripcion": "TEST-B MINISTERIO DG UNO", "children": []}


async def test_organos_filtra_por_nivel(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    assert [r["id"] for r in await propios(client_usuario, "/catalogos/organos", nivel="ccaa")] == [BASE + 4]
    assert [r["id"] for r in await propios(client_usuario, "/catalogos/organos", nivel="estado")] == [BASE + 1]
    assert (await client_usuario.get("/catalogos/organos", params={"nivel": "marte"})).status_code == 422


async def test_regiones_anidan_provincias_y_no_mezclan_organos(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    raices = await propios(client_usuario, "/catalogos/regiones")
    assert [r["id"] for r in raices] == [BASE + 10]
    assert [h["id"] for h in raices[0]["children"]] == [BASE + 11]


async def test_los_ids_no_cambian_tras_un_refresco(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    antes = await propios(client_usuario, "/catalogos/regiones")
    async with AsyncSessionLocal() as db:  # un refresco es un upsert por (tipo, id): se actualiza, no se renumera
        provincia = await db.get(CatalogoBdns, ("region", BASE + 11))
        assert provincia is not None
        provincia.codigo = "ES990"
        await db.commit()
    assert await propios(client_usuario, "/catalogos/regiones") == antes


async def test_beneficiarios_distintos_sin_las_retiradas(client_usuario: AsyncClient) -> None:
    await crear_convocatoria_en_bd(beneficiarios=["TEST-B PYME", "TEST-B AUTONOMO"])
    await crear_convocatoria_en_bd(beneficiarios=["TEST-B PYME"])
    await crear_convocatoria_en_bd(beneficiarios=["TEST-B RETIRADO"], removed_at=datetime.now(UTC))
    nodos = await propios(client_usuario, "/catalogos/beneficiarios")
    assert [n["descripcion"] for n in nodos] == ["TEST-B AUTONOMO", "TEST-B PYME"]
    assert all(n["children"] == [] and n["id"] == n["descripcion"] for n in nodos)  # el filtro usa el texto tal cual


async def test_no_hay_ruta_de_acciones(client_usuario: AsyncClient) -> None:
    assert "/catalogos/acciones" not in {getattr(r, "path", "") for r in router.routes}
    assert (await client_usuario.get("/catalogos/acciones")).status_code == 404


@pytest.mark.parametrize("ruta", RUTAS)
async def test_anonimo_recibe_401(client: AsyncClient, ruta: str) -> None:
    assert (await client.get(ruta)).status_code == 401


@pytest.mark.parametrize("ruta", RUTAS)
async def test_cualquier_rol_autenticado_recibe_200(client_usuario: AsyncClient, ruta: str) -> None:
    assert (await client_usuario.get(ruta)).status_code == 200
