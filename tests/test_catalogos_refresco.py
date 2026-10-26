"""Catalog refresh against the real database (PR2, task 2.5).

The BDNS is a `MockTransport`. Rows use synthetic ids far above the real ones and
`TEST-` descriptions, signed by a test user, so the teardown in `conftest.py`
removes them without touching a catalog that a real sync may have filled.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import CatalogoBdns
from app.services.bdns_cliente import BdnsNoDisponible
from app.services.bdns_mapper import RutaOrgano
from app.services.bdns_sync_cliente import ClienteBdnsSync
from app.services.catalogos import cargar_resolvedor, refrescar_catalogos
from tests.conftest import Sesion

BASE = 9_100_000


def nodo(offset: int, descripcion: str, *hijos: dict[str, Any]) -> dict[str, Any]:
    return {"id": BASE + offset, "descripcion": f"TEST-{descripcion}", "children": list(hijos)}


class FalsaBdns:
    def __init__(self) -> None:
        self.organos_estado: list[dict[str, Any]] = [nodo(1, "MINISTERIO X", nodo(2, "DG UNO"), nodo(3, "DG DOS"))]
        self.regiones: list[dict[str, Any]] = [nodo(10, "ES99 - REGION", nodo(11, "ES990 - PROVINCIA"))]
        self.peticiones: list[httpx.URL] = []

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion.url)
        if peticion.url.path.endswith("/regiones"):
            return httpx.Response(200, json=self.regiones)
        if peticion.url.params["idAdmon"] == "C":
            return httpx.Response(200, json=self.organos_estado)
        return httpx.Response(200, json=[])


def cliente_de(bdns: FalsaBdns) -> ClienteBdnsSync:
    return ClienteBdnsSync(
        httpx.AsyncClient(transport=httpx.MockTransport(bdns), base_url="https://bdns.example/api"),
        rps=4.0,
    )


async def filas_de_prueba() -> dict[tuple[str, int], CatalogoBdns]:
    async with AsyncSessionLocal() as db:
        filas = (await db.execute(select(CatalogoBdns).where(CatalogoBdns.bdns_id >= BASE))).scalars().all()
        return {(f.tipo, f.bdns_id): f for f in filas}


async def test_refrescar_hace_cinco_llamadas_y_guarda_arboles_firmados_por_el_usuario(admin: Sesion) -> None:
    bdns = FalsaBdns()
    async with cliente_de(bdns) as cliente, AsyncSessionLocal() as db:
        resultado = await refrescar_catalogos(db, cliente, usuario_id=admin.id)

    assert len(bdns.peticiones) == 5
    assert sorted(p.params["idAdmon"] for p in bdns.peticiones if "idAdmon" in p.params) == ["A", "C", "L", "O"]
    assert (resultado.organos, resultado.regiones) == (3, 2)
    filas = await filas_de_prueba()
    assert set(filas) == {
        ("organo", BASE + 1),
        ("organo", BASE + 2),
        ("organo", BASE + 3),
        ("region", BASE + 10),
        ("region", BASE + 11),
    }
    dg = filas[("organo", BASE + 2)]
    assert dg.padre_bdns_id == BASE + 1
    assert dg.ruta_ids == [BASE + 1, BASE + 2]
    assert dg.ruta_norm == "TEST-MINISTERIO X / TEST-DG UNO"
    assert dg.nivel_administracion == "estado"
    assert (dg.created_by, dg.updated_by) == (admin.id, admin.id)
    provincia = filas[("region", BASE + 11)]
    assert provincia.codigo is None  # "TEST-ES990..." does not look like a NUTS code
    assert provincia.ruta_ids == [BASE + 10, BASE + 11]


async def test_refrescar_dos_veces_actualiza_sin_duplicar_y_nunca_borra(admin: Sesion) -> None:
    bdns = FalsaBdns()
    async with cliente_de(bdns) as cliente, AsyncSessionLocal() as db:
        await refrescar_catalogos(db, cliente, usuario_id=admin.id)
    primeras = await filas_de_prueba()

    # Second run: one node renamed, one removed upstream.
    bdns.organos_estado = [nodo(1, "MINISTERIO X RENOMBRADO", nodo(2, "DG UNO"))]
    async with cliente_de(bdns) as cliente, AsyncSessionLocal() as db:
        await refrescar_catalogos(db, cliente, usuario_id=admin.id)
    segundas = await filas_de_prueba()

    assert set(segundas) == set(primeras)  # DG DOS is kept: alerts store those ids
    assert segundas[("organo", BASE + 1)].descripcion == "TEST-MINISTERIO X RENOMBRADO"
    assert segundas[("organo", BASE + 2)].ruta_norm == "TEST-MINISTERIO X RENOMBRADO / TEST-DG UNO"
    assert segundas[("organo", BASE + 3)].descripcion == "TEST-DG DOS"
    assert segundas[("organo", BASE + 1)].sincronizado_at > primeras[("organo", BASE + 1)].sincronizado_at
    assert segundas[("organo", BASE + 1)].created_at == primeras[("organo", BASE + 1)].created_at


async def test_si_la_bdns_falla_a_mitad_no_se_escribe_nada(admin: Sesion) -> None:
    bdns = FalsaBdns()

    def rota(peticion: httpx.Request) -> httpx.Response:
        if peticion.url.path.endswith("/regiones"):
            return httpx.Response(400)
        return bdns(peticion)

    cliente = ClienteBdnsSync(
        httpx.AsyncClient(transport=httpx.MockTransport(rota), base_url="https://bdns.example/api"), rps=4.0
    )
    async with cliente, AsyncSessionLocal() as db:
        with pytest.raises(BdnsNoDisponible):
            await refrescar_catalogos(db, cliente, usuario_id=admin.id)

    assert await filas_de_prueba() == {}


async def test_el_resolvedor_se_carga_de_la_base_de_datos(admin: Sesion) -> None:
    bdns = FalsaBdns()
    async with cliente_de(bdns) as cliente, AsyncSessionLocal() as db:
        await refrescar_catalogos(db, cliente, usuario_id=admin.id)

    async with AsyncSessionLocal() as db:
        resolvedor = await cargar_resolvedor(db)

    assert resolvedor.resolver_organo(RutaOrgano("ESTADO", "test-ministerio x", "TEST-DG DOS")) == [BASE + 1, BASE + 3]
    assert resolvedor.resolver_organo(RutaOrgano("ESTADO", "TEST-MINISTERIO X", "TEST-NO EXISTE")) == [BASE + 1]
    assert resolvedor.organos_sin_resolver == 1
