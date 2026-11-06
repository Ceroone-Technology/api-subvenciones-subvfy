"""Radar and `orden=afinidad` (N3): the score over real rows, the prefilter, the contract and tenant scope.

The shared database holds synced grants, so the exact-score tests read the radar through the service (no limit) and
look their rows up by code; the HTTP tests tag titles with a unique word (`marca`) or filter by the code they created.
"""

import random
import string
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.models import CatalogoBdns, Empresa, EmpresaInteres
from app.services import radar as servicio
from app.services.convocatoria_estado import hoy_madrid
from tests.conftest import MARCA_TEST, Sesion, crear_convocatoria_en_bd

BENEFICIARIO_PYME = "PYME Y PERSONAS FÍSICAS QUE DESARROLLAN ACTIVIDAD ECONÓMICA"
# Region ids that cannot clash with the BDNS ones: nodes of the test catalogue (`TEST-` is cleaned by the teardown).
REGION_VALENCIA, REGION_GALICIA, REGION_RAIZ = 9_100_001, 9_100_002, 9_100_003


def marca() -> str:
    return "zz" + "".join(random.choices(string.ascii_lowercase, k=12))


async def hoy():  # noqa: ANN201
    async with AsyncSessionLocal() as db:
        return await db.scalar(select(hoy_madrid()))


async def sembrar_regiones() -> None:
    async with AsyncSessionLocal() as db:
        for bdns_id, codigo, nombre in (
            (REGION_VALENCIA, "ES52", "Valencia"),
            (REGION_GALICIA, "ES11", "Galicia"),
            (REGION_RAIZ, "ES", "Espana"),
        ):
            db.add(CatalogoBdns(tipo="region", bdns_id=bdns_id, descripcion=f"{MARCA_TEST}{nombre}", codigo=codigo))
        await db.commit()


async def perfilar(empresa_id: int, usuario_id: int, *, intereses: tuple[str, ...] = ("erp",), **campos: Any) -> None:
    datos = {"actividad_codigo": "C", "tamano": "pequena", "provincia": "Valencia"} | campos
    async with AsyncSessionLocal() as db:
        await db.execute(update(Empresa).where(Empresa.id == empresa_id).values(**datos))
        for codigo in intereses:
            db.add(EmpresaInteres(empresa_id=empresa_id, interes_codigo=codigo, created_by=usuario_id))
        await db.commit()


async def sembrar_convocatoria(**extra: Any) -> str:
    dia = await hoy()
    base: dict[str, Any] = {
        "fecha_registro": dia,
        "fecha_inicio_solicitud": dia - timedelta(days=3),
        "fecha_fin_solicitud": dia + timedelta(days=40),
        "tema": "dig",
        "cnae_secciones": ["C"],
        "sectores": ["C"],
        "region_ids": [REGION_RAIZ, REGION_VALENCIA],
        "beneficiarios": [BENEFICIARIO_PYME],
        "nivel_administracion": "ccaa",
        "organo_convocante": "Conselleria de Prueba",
        "presupuesto_total": 120000,
    }
    titulo = extra.pop("titulo", "Ayudas")
    return (await crear_convocatoria_en_bd(titulo, **(base | extra))).codigo_bdns


async def radar_de(empresa_id: int, **filtros: Any) -> dict[str, Any]:
    """Every opportunity of the radar by code (no limit)."""
    async with AsyncSessionLocal() as db:
        _, oportunidades = await servicio.radar(db, empresa_id, limite=10**6, **filtros)
    return {o.fila["codigo_bdns"]: o for o in oportunidades}


# --- Score over real rows ---------------------------------------------------------------------------------


async def test_una_convocatoria_que_encaja_en_todo_puntua_cien(empresa: dict, usuario_raso: Sesion) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    perfecta = await sembrar_convocatoria()
    o = (await radar_de(empresa["id"]))[perfecta]
    assert o.resultado.afinidad == 100
    assert o.resultado.zona == "val"
    assert o.resultado.dudas == []
    assert o.resultado.motivos == [
        "Encaja con la actividad de tu empresa",
        "Sede en C. Valenciana",
        "Dirigida a pymes y autónomos",
        "Interés: erp y gestión",
    ]


async def test_cada_bloque_resta_lo_suyo_y_el_orden_es_el_de_afinidad(empresa: dict, usuario_raso: Sesion) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    dia = await hoy()
    perfecta = await sembrar_convocatoria()
    otra_zona = await sembrar_convocatoria(region_ids=[REGION_RAIZ, REGION_GALICIA])
    estatal = await sembrar_convocatoria(nivel_administracion="estado", region_ids=[REGION_RAIZ])
    urgente = await sembrar_convocatoria(fecha_fin_solicitud=dia + timedelta(days=3))
    otro_tema = await sembrar_convocatoria(tema="ene")
    sin_plazo = await sembrar_convocatoria(
        fecha_fin_solicitud=None, plazo_permanente=True, fecha_inicio_solicitud=dia - timedelta(days=1)
    )
    todas = await radar_de(empresa["id"])
    puntos = {nombre: todas[codigo].resultado.afinidad for nombre, codigo in (
        ("perfecta", perfecta), ("otra_zona", otra_zona), ("estatal", estatal), ("urgente", urgente),
        ("otro_tema", otro_tema), ("sin_plazo", sin_plazo),
    )}  # fmt: skip
    assert puntos == {
        "perfecta": 100, "otra_zona": 80, "estatal": 94, "urgente": 92, "otro_tema": 85, "sin_plazo": 95,
    }  # fmt: skip
    assert todas[urgente].dias_restantes == 3 and todas[sin_plazo].dias_restantes is None
    esperado = [perfecta, sin_plazo, estatal, urgente, otro_tema, otra_zona]
    assert [c for c in todas if c in set(esperado)] == esperado


async def test_el_prefiltro_deja_fuera_cerradas_retiradas_y_de_otro_sector(empresa: dict, usuario_raso: Sesion) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    dia = await hoy()
    abierta = await sembrar_convocatoria()
    cerrada = await sembrar_convocatoria(fecha_fin_solicitud=dia - timedelta(days=1))
    retirada = await sembrar_convocatoria(removed_at=datetime.now(UTC))
    otro_sector = await sembrar_convocatoria(cnae_secciones=["A"], sectores=["A"])
    abierta_a_todos = await sembrar_convocatoria(cnae_secciones=[], sectores=[])
    todas = await radar_de(empresa["id"])
    assert abierta in todas and abierta_a_todos in todas
    assert not {cerrada, retirada, otro_sector} & set(todas)
    assert todas[abierta_a_todos].resultado.motivos[0] == "Abierta a todos los sectores"


async def test_sin_actividad_no_hay_prefiltro_de_sector_y_se_pide_el_dato(empresa: dict, usuario_raso: Sesion) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id, actividad_codigo=None)
    otro_sector = await sembrar_convocatoria(cnae_secciones=["A"], sectores=["A"])
    o = (await radar_de(empresa["id"]))[otro_sector]
    assert o.resultado.dudas[0] == "Indica la actividad principal (CNAE) de tu empresa para comprobar el sector"


async def test_filtros_min_tema_y_gestiona(empresa: dict, usuario_raso: Sesion) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    buena = await sembrar_convocatoria(gestiona_subvfy=True)
    floja = await sembrar_convocatoria(tema="ene", region_ids=[REGION_RAIZ, REGION_GALICIA])
    assert {buena, floja} <= set(await radar_de(empresa["id"]))
    assert buena in await radar_de(empresa["id"], minimo=100) and floja not in await radar_de(empresa["id"], minimo=100)
    solo_ene = await radar_de(empresa["id"], temas=("ene",))
    assert floja in solo_ene and buena not in solo_ene
    gestionadas = await radar_de(empresa["id"], gestiona=True)
    assert buena in gestionadas and floja not in gestionadas


async def test_el_perfil_de_otra_empresa_da_otra_afinidad(
    empresa: dict, usuario_raso: Sesion, otra_empresa: dict
) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    codigo = await sembrar_convocatoria()
    suya = (await radar_de(empresa["id"]))[codigo].resultado.afinidad
    vacia = (await radar_de(otra_empresa["id"]))[codigo].resultado.afinidad
    assert suya == 100 and vacia < suya


# --- HTTP ----------------------------------------------------------------------------------------------------


async def test_radar_exige_autenticacion(client: AsyncClient) -> None:
    assert (await client.get("/radar")).status_code == 401


async def test_radar_cualquier_rol_y_forma_de_la_respuesta(
    client_usuario: AsyncClient,
    client_gestor: AsyncClient,
    client_admin: AsyncClient,
    empresa: dict,
    usuario_raso: Sesion,
) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    codigo = await sembrar_convocatoria(titulo="Radar de forma")
    for cliente in (client_usuario, client_gestor, client_admin):
        assert (await cliente.get("/radar", params={"limit": 1})).status_code == 200
    r = await client_usuario.get("/radar", params={"min": 100, "limit": 200})
    assert r.status_code == 200
    cuerpo = r.json()
    assert set(cuerpo) == {"items", "total"} and cuerpo["total"] >= len(cuerpo["items"]) >= 1
    item = next(i for i in cuerpo["items"] if i["codigo_bdns"] == codigo)
    assert set(item) == {
        "codigo_bdns", "titulo", "organo", "tema", "gestiona_subvfy", "afinidad", "motivos", "dudas",
        "dias_restantes", "presupuesto_total", "zona",
    }  # fmt: skip
    assert item["titulo"] == "Radar de forma" and item["organo"] == "Conselleria de Prueba"
    assert (item["tema"], item["afinidad"], item["zona"], item["presupuesto_total"]) == ("dig", 100, "val", 120000.0)
    assert item["dias_restantes"] in (39, 40) and item["gestiona_subvfy"] is False


async def test_radar_limit_min_y_validaciones(client_usuario: AsyncClient) -> None:
    r = await client_usuario.get("/radar", params={"limit": 2})
    assert r.status_code == 200 and len(r.json()["items"]) <= 2
    afinidades = [i["afinidad"] for i in (await client_usuario.get("/radar", params={"limit": 50})).json()["items"]]
    assert afinidades == sorted(afinidades, reverse=True)
    alta = (await client_usuario.get("/radar", params={"min": 90, "limit": 200})).json()["items"]
    assert all(i["afinidad"] >= 90 for i in alta)
    for malos in ({"min": 101}, {"min": -1}, {"limit": 0}, {"limit": 201}, {"tema": "nope"}):
        assert (await client_usuario.get("/radar", params=malos)).status_code == 422


async def test_radar_filtra_por_tema_y_gestiona(client_usuario: AsyncClient) -> None:
    items = (await client_usuario.get("/radar", params={"tema": ["ene", "int"], "limit": 200})).json()["items"]
    assert all(i["tema"] in ("ene", "int") for i in items)
    items = (await client_usuario.get("/radar", params={"gestiona": "true", "limit": 200})).json()["items"]
    assert all(i["gestiona_subvfy"] for i in items)


# --- orden=afinidad in the search ------------------------------------------------------------------------


async def test_orden_afinidad_ordena_y_pagina_con_la_afinidad_de_cada_fila(
    client_usuario: AsyncClient, empresa: dict, usuario_raso: Sesion
) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    m = marca()
    media = await sembrar_convocatoria(titulo=f"Media {m}", region_ids=[REGION_RAIZ, REGION_GALICIA])
    mejor = await sembrar_convocatoria(titulo=f"Mejor {m}")
    peor = await sembrar_convocatoria(
        titulo=f"Peor {m}", tema="otro", region_ids=[REGION_RAIZ, REGION_GALICIA], cnae_secciones=["A"], sectores=["A"]
    )
    r = await client_usuario.get("/convocatorias", params={"q": m, "orden": "afinidad"})
    assert r.status_code == 200, r.text
    assert [(i["codigo_bdns"], i["afinidad"]) for i in r.json()["items"]] == [(mejor, 100), (media, 80), (peor, 25)]
    assert r.json()["total"] == 3
    por_pagina = {"q": m, "orden": "afinidad", "size": 1}
    segunda = (await client_usuario.get("/convocatorias", params=por_pagina | {"page": 2})).json()
    assert [i["codigo_bdns"] for i in segunda["items"]] == [media] and segunda["total"] == 3
    fuera = (await client_usuario.get("/convocatorias", params=por_pagina | {"page": 4})).json()
    assert fuera["items"] == [] and fuera["total"] == 3


async def test_sin_orden_afinidad_la_fila_no_trae_afinidad_y_la_exportacion_no_la_admite(
    client_usuario: AsyncClient,
) -> None:
    m = marca()
    await sembrar_convocatoria(titulo=f"Normal {m}")
    fila = (await client_usuario.get("/convocatorias", params={"q": m})).json()["items"][0]
    assert fila["afinidad"] is None
    r = await client_usuario.get("/convocatorias/export", params={"formato": "csv", "orden": "afinidad"})
    assert r.status_code == 422
