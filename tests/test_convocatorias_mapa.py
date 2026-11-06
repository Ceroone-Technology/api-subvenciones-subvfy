"""`GET /convocatorias/mapa`: totals per community, nationwide and without zone, over the filtered set.

The database is shared with real grants, so every test tags titles with a unique word (`marca`) and filters by it.
Region nodes use ids that cannot clash with the BDNS ones; the teardown removes the `TEST-` catalogue rows.
"""

import random
import string
from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import CatalogoBdns
from app.services import mapa_convocatorias as servicio
from app.services.convocatoria_estado import hoy_madrid
from tests.conftest import MARCA_TEST, crear_convocatoria_en_bd

VAL_PROVINCIA, VAL_COMUNIDAD, GAL_COMUNIDAD, RAIZ = 9_200_001, 9_200_002, 9_200_003, 9_200_004


def marca() -> str:
    return "zz" + "".join(random.choices(string.ascii_lowercase, k=12))


async def hoy():  # noqa: ANN201
    async with AsyncSessionLocal() as db:
        return await db.scalar(select(hoy_madrid()))


async def sembrar_catalogo() -> None:
    async with AsyncSessionLocal() as db:
        for bdns_id, codigo, nombre in (
            (VAL_COMUNIDAD, "ES52", "Valencia"),
            (GAL_COMUNIDAD, "ES11", "Galicia"),
            (RAIZ, "ES", "Espana"),
        ):
            existe = await db.get(CatalogoBdns, ("region", bdns_id))
            if existe is None:
                db.add(
                    CatalogoBdns(tipo="region", bdns_id=bdns_id, descripcion=f"{MARCA_TEST}{nombre}", codigo=codigo)
                )
        await db.commit()
    servicio.limpiar_cache()


async def sembrar(m: str, **extra: Any) -> str:
    dia = await hoy()
    base: dict[str, Any] = {
        "fecha_registro": dia,
        "fecha_inicio_solicitud": dia - timedelta(days=3),
        "fecha_fin_solicitud": dia + timedelta(days=20),
        "presupuesto_total": 100,
    }
    return (await crear_convocatoria_en_bd(f"Ayudas {m}", **(base | extra))).codigo_bdns


async def mapa(client: AsyncClient, m: str, **params: Any) -> dict[str, Any]:
    r = await client.get("/convocatorias/mapa", params={"q": m, **params})
    assert r.status_code == 200, r.text
    return r.json()


def comunidad(datos: dict[str, Any], nombre: str) -> dict[str, Any]:
    return next(c for c in datos["por_comunidad"] if c["nombre"] == nombre)


async def test_requiere_login(client: AsyncClient) -> None:
    assert (await client.get("/convocatorias/mapa")).status_code == 401


async def test_ruta_no_se_confunde_con_el_detalle(client_usuario: AsyncClient) -> None:
    r = await client_usuario.get("/convocatorias/mapa")
    assert r.status_code == 200
    assert set(r.json()) == {"total", "presupuesto_total", "por_comunidad", "estatal", "sin_zona", "exterior"}


async def test_reparte_por_comunidad_estatal_y_sin_zona(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    m = marca()
    dia = await hoy()
    # Valencia: province + community ids (rolls up once), open, 100.
    await sembrar(m, region_ids=[VAL_PROVINCIA, VAL_COMUNIDAD, RAIZ], presupuesto_total=100)
    # Valencia and Galicia, closed, 50: counts in each community.
    await sembrar(
        m,
        region_ids=[VAL_COMUNIDAD, GAL_COMUNIDAD],
        presupuesto_total=50,
        fecha_fin_solicitud=dia - timedelta(days=2),
    )
    # State-level grant with no regions.
    await sembrar(m, nivel_administracion="estado", presupuesto_total=1000)
    # No zone at all, no budget.
    await sembrar(m, presupuesto_total=None)
    datos = await mapa(client_usuario, m)
    assert datos["total"] == 4
    assert datos["presupuesto_total"] == 1150
    val, gal = comunidad(datos, "C. Valenciana"), comunidad(datos, "Galicia")
    assert (val["total"], val["abiertas"], val["presupuesto"]) == (2, 1, 150)
    assert (gal["total"], gal["abiertas"], gal["presupuesto"]) == (1, 0, 50)
    assert isinstance(val["region_id"], int)
    assert datos["estatal"] == {"total": 1, "abiertas": 1, "presupuesto": 1000}
    assert datos["sin_zona"] == {"total": 1}
    assert [c["nombre"] for c in datos["por_comunidad"]] == ["C. Valenciana", "Galicia"]  # biggest first


async def test_la_suma_de_comunidades_puede_superar_el_total(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    m = marca()
    await sembrar(m, region_ids=[VAL_COMUNIDAD, GAL_COMUNIDAD])
    datos = await mapa(client_usuario, m)
    assert datos["total"] == 1
    assert sum(c["total"] for c in datos["por_comunidad"]) == 2


async def test_aplica_los_mismos_filtros_que_el_listado(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    m = marca()
    await sembrar(m, region_ids=[VAL_COMUNIDAD], presupuesto_total=10)
    await sembrar(m, region_ids=[GAL_COMUNIDAD], presupuesto_total=900)
    datos = await mapa(client_usuario, m, presupuesto_min=500)
    assert datos["total"] == 1
    assert [c["nombre"] for c in datos["por_comunidad"]] == ["Galicia"]
    # Page, size and order are accepted and ignored.
    ignorados = await mapa(client_usuario, m, page=3, size=1, orden="fondos")
    assert ignorados["total"] == 2


async def test_vacio_devuelve_ceros(client_usuario: AsyncClient) -> None:
    datos = await mapa(client_usuario, marca())
    assert datos == {
        "total": 0,
        "presupuesto_total": 0,
        "por_comunidad": [],
        "estatal": {"total": 0, "abiertas": 0, "presupuesto": 0},
        "sin_zona": {"total": 0},
        "exterior": {"total": 0, "abiertas": 0, "presupuesto": 0},
    }


async def test_la_cache_sirve_lo_mismo_dentro_del_ttl_y_se_puede_limpiar(client_usuario: AsyncClient) -> None:
    await sembrar_catalogo()
    m = marca()
    await sembrar(m, region_ids=[VAL_COMUNIDAD])
    assert (await mapa(client_usuario, m))["total"] == 1
    await sembrar(m, region_ids=[VAL_COMUNIDAD])
    assert (await mapa(client_usuario, m))["total"] == 1  # cached
    servicio.limpiar_cache()
    assert (await mapa(client_usuario, m))["total"] == 2


def test_la_cache_esta_acotada() -> None:
    servicio.limpiar_cache()
    for i in range(servicio.CACHE_MAX_ENTRADAS + 20):
        servicio._guardar(("k", i), {"total": i})
    assert servicio.tamano_cache() <= servicio.CACHE_MAX_ENTRADAS
    servicio.limpiar_cache()


# Test copies of the real region nodes (codes and parents as in the BDNS catalogue).
UK, UKZ, TODO_EL_MUNDO = 9_201_001, 9_201_002, 9_201_003
ES5, ES51, ES52, ES53 = 9_201_010, 9_201_011, 9_201_012, 9_201_013


async def sembrar_catalogo_exterior() -> None:
    await sembrar_catalogo()
    nodos = (
        (UK, "UK", "UK - UNITED KINGDOM", None),
        (UKZ, "UKZ", "UKZ - EXTRA-REGIO NUTS 1", UK),
        (TODO_EL_MUNDO, "XXXX", "XXXX - TODO EL MUNDO", None),
        (ES5, "ES5", "ES5 - ESTE", RAIZ),
        (ES51, "ES51", "ES51 - CATALUNA", ES5),
        (ES52, "ES52", "ES52 - COMUNIDAD VALENCIANA", ES5),
        (ES53, "ES53", "ES53 - ILLES BALEARS", ES5),
    )
    async with AsyncSessionLocal() as db:
        for bdns_id, codigo, nombre, padre in nodos:
            if await db.get(CatalogoBdns, ("region", bdns_id)) is None:
                db.add(
                    CatalogoBdns(
                        tipo="region", bdns_id=bdns_id, codigo=codigo, descripcion=f"{MARCA_TEST}{nombre}",
                        padre_bdns_id=padre,
                    )  # fmt: skip
                )
        await db.commit()
    servicio.limpiar_cache()


async def test_exterior_macrorregiones_y_estatal(client_usuario: AsyncClient) -> None:
    """Abroad (938040 UKZ, XXXX) is its own bucket; the ES5 macro rolls up; only region ES is national."""
    await sembrar_catalogo_exterior()
    m = marca()
    await sembrar(m, region_ids=[UK, UKZ], nivel_administracion="estado")  # 938040-like: Spanish grant, project abroad
    await sembrar(m, region_ids=[TODO_EL_MUNDO], nivel_administracion="local")
    await sembrar(m, region_ids=[RAIZ], nivel_administracion="local")  # region España only: national
    await sembrar(m, region_ids=[RAIZ, ES5])  # macro only: Cataluña, C. Valenciana and Illes Balears
    await sembrar(m, region_ids=[RAIZ, ES5, ES52])  # the macro is just an ancestor: only C. Valenciana
    await sembrar(m, region_ids=[UK, ES52])  # mixed: a Spanish grant, counted in its community
    await sembrar(m, region_ids=[], nivel_administracion="local")  # no zone
    datos = await mapa(client_usuario, m)

    assert datos["total"] == 7
    assert datos["exterior"] == {"total": 2, "abiertas": 2, "presupuesto": 200}
    assert datos["estatal"]["total"] == 1  # the "estado" grant abroad is NOT national
    assert datos["sin_zona"] == {"total": 1}
    assert comunidad(datos, "C. Valenciana")["total"] == 3
    assert comunidad(datos, "Cataluña")["total"] == 1
    assert comunidad(datos, "Illes Balears")["total"] == 1
    assert {c["nombre"] for c in datos["por_comunidad"]} == {"C. Valenciana", "Cataluña", "Illes Balears"}
