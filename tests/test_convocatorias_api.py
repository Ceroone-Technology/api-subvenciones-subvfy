"""Authenticated search, detail and summary over the local table (PR4, tasks 4.1 and 4.2).

Tests share the real database with synced grants, so every test tags its rows with a
unique title word (`marca`) and searches with it: results never include rows it did not create.
"""

import random
import string
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.api.routes.convocatorias import router
from app.database import AsyncSessionLocal, engine
from app.main import app
from app.models import SyncRun
from tests.conftest import Sesion, crear_convocatoria_en_bd, crear_sesion_en_bd

HOY = date.today()


def marca() -> str:
    return "zz" + "".join(random.choices(string.ascii_lowercase, k=12))


async def sembrar(m: str, titulo: str = "Ayudas", **extra: Any) -> str:
    fila = await crear_convocatoria_en_bd(f"{titulo} {m}", **extra)
    return fila.codigo_bdns


async def buscar(client: AsyncClient, m: str, **params: Any) -> dict[str, Any]:
    respuesta = await client.get("/convocatorias", params={"q": m, **params})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


async def codigos(client: AsyncClient, m: str, **params: Any) -> set[str]:
    return {i["codigo_bdns"] for i in (await buscar(client, m, **params))["items"]}


@contextmanager
def contar_sentencias() -> Iterator[list[str]]:
    vistas: list[str] = []

    def vigilar(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        vistas.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", vigilar)
    try:
        yield vistas
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", vigilar)


# --- Search -----------------------------------------------------------------


async def test_texto_sin_acentos_encuentra_con_acentos(client_usuario: AsyncClient) -> None:
    m = marca()
    innovacion = await sembrar(m, "Ayudas a la innovación")
    await sembrar(m, "Ayudas a la agricultura")
    assert await codigos(client_usuario, f"innovacion {m}") == {innovacion}
    assert await codigos(client_usuario, f"innovación {m}") == {innovacion}


async def test_cada_filtro_acota_el_resultado(client_usuario: AsyncClient) -> None:
    m = marca()
    ayer = HOY - timedelta(days=1)
    a = await sembrar(
        m, nivel_administracion="estado", organo_ids=[7001, 7002], region_ids=[8001], beneficiarios=["PYME"],
        fecha_registro=ayer, fecha_inicio_solicitud=ayer, fecha_fin_solicitud=HOY + timedelta(days=5),
        presupuesto_total=1000, financiada_mrr=True,
    )  # fmt: skip
    b = await sembrar(
        m, nivel_administracion="local", organo_ids=[7003], region_ids=[8002], beneficiarios=["Autónomos"],
        fecha_registro=HOY, fecha_fin_solicitud=HOY - timedelta(days=5), presupuesto_total=50_000,
    )  # fmt: skip
    c = await sembrar(m, fecha_registro=HOY)  # sin dato en ningún filtro
    assert await codigos(client_usuario, m) == {a, b, c}
    assert await codigos(client_usuario, m, nivel="estado") == {a}
    assert await codigos(client_usuario, m, organo=[7002, 7003]) == {a, b}
    assert await codigos(client_usuario, m, organo=7001) == {a}
    assert await codigos(client_usuario, m, region=8002) == {b}
    assert await codigos(client_usuario, m, beneficiario="Autónomos") == {b}
    assert await codigos(client_usuario, m, estado="Abierta") == {a}
    assert await codigos(client_usuario, m, estado=["Cerrada", "Plazo no especificado"]) == {b, c}
    assert await codigos(client_usuario, m, registro_desde=HOY.isoformat()) == {b, c}
    assert await codigos(client_usuario, m, registro_hasta=ayer.isoformat()) == {a}
    assert await codigos(client_usuario, m, plazo_desde=HOY.isoformat()) == {a}
    assert await codigos(client_usuario, m, plazo_hasta=(HOY - timedelta(days=1)).isoformat()) == {b}
    assert await codigos(client_usuario, m, presupuesto_min=2000) == {b}
    assert await codigos(client_usuario, m, presupuesto_max=2000) == {a}
    assert await codigos(client_usuario, m, solo_mrr=True) == {a}
    assert await codigos(client_usuario, m, nivel="estado", organo=7003) == set()  # los filtros se acumulan


async def test_paginas_estables_sin_repetidos_ni_saltos(client_usuario: AsyncClient) -> None:
    m = marca()
    mismo_dia = HOY - timedelta(days=3)  # empate total: solo el desempate por id ordena
    esperados = {await sembrar(m, fecha_registro=mismo_dia) for _ in range(25)}
    vistos: list[str] = []
    for pagina in (1, 2, 3):
        cuerpo = await buscar(client_usuario, m, page=pagina, size=10)
        assert cuerpo["total"] == 25 and cuerpo["page"] == pagina and cuerpo["size"] == 10
        vistos += [i["codigo_bdns"] for i in cuerpo["items"]]
    assert len(vistos) == 25 and set(vistos) == esperados
    assert (await buscar(client_usuario, m, page=4, size=10))["items"] == []


async def test_las_retiradas_no_salen_pero_el_detalle_las_marca(client_usuario: AsyncClient) -> None:
    m = marca()
    viva = await sembrar(m)
    retirada = await sembrar(m, removed_at=datetime.now(UTC))
    assert await codigos(client_usuario, m) == {viva}
    detalle = await client_usuario.get(f"/convocatorias/{retirada}")
    assert detalle.status_code == 200 and detalle.json()["retirada"] is True
    assert (await client_usuario.get(f"/convocatorias/{viva}")).json()["retirada"] is False


async def test_estado_filtra_en_todas_las_paginas_y_el_total_lo_refleja(client_usuario: AsyncClient) -> None:
    m = marca()
    abiertas = {await sembrar(m, fecha_fin_solicitud=HOY + timedelta(days=9)) for _ in range(5)}
    for _ in range(4):
        await sembrar(m, fecha_fin_solicitud=HOY - timedelta(days=9))
    vistos: set[str] = set()
    for pagina in (1, 2, 3):
        cuerpo = await buscar(client_usuario, m, estado="Abierta", page=pagina, size=2)
        assert cuerpo["total"] == 5
        assert cuerpo["items"] and {i["estado"] for i in cuerpo["items"]} == {"Abierta"}
        vistos |= {i["codigo_bdns"] for i in cuerpo["items"]}
    assert vistos == abiertas


async def test_sin_fechas_se_listan_como_plazo_no_especificado(client_usuario: AsyncClient) -> None:
    m = marca()
    sin_nada = await sembrar(m)
    permanente = await sembrar(m, plazo_permanente=True)
    items = {i["codigo_bdns"]: i for i in (await buscar(client_usuario, m))["items"]}
    assert items[sin_nada]["estado"] == "Plazo no especificado"
    assert items[permanente]["estado"] == "Abierta sin plazo"
    assert await codigos(client_usuario, m, estado="Plazo no especificado") == {sin_nada}
    assert await codigos(client_usuario, m, estado="Abierta sin plazo") == {permanente}


async def test_bandera_obsoleta_de_la_bdns_no_manda(client_usuario: AsyncClient) -> None:
    m = marca()
    await sembrar(
        m,
        abierto_bdns=False,
        fecha_inicio_solicitud=HOY + timedelta(days=3),
        fecha_fin_solicitud=HOY + timedelta(days=9),
    )
    (item,) = (await buscar(client_usuario, m))["items"]
    assert item["estado"] == "Pendiente"


async def test_orden_por_fondos_recientes_antiguas_y_codigo(client_usuario: AsyncClient) -> None:
    m = marca()
    barata = await sembrar(m, presupuesto_total=10, fecha_registro=HOY - timedelta(days=1))
    cara = await sembrar(m, presupuesto_total=900, fecha_registro=HOY - timedelta(days=5))
    sin_dato = await sembrar(m, fecha_registro=HOY - timedelta(days=9))

    async def orden(nombre: str) -> list[str]:
        return [i["codigo_bdns"] for i in (await buscar(client_usuario, m, orden=nombre))["items"]]

    assert await orden("fondos") == [cara, barata, sin_dato]  # nulos al final
    assert await orden("recientes") == [barata, cara, sin_dato]
    assert await orden("antiguas") == [sin_dato, cara, barata]
    por_codigo = await orden("codigo")
    assert por_codigo == sorted([barata, cara, sin_dato])


async def test_relevancia_pone_antes_el_titulo_que_la_finalidad(client_usuario: AsyncClient) -> None:
    m = marca()
    en_finalidad = (await crear_convocatoria_en_bd("Programa general", finalidad=m)).codigo_bdns
    en_titulo = await sembrar(m, "Programa")
    cuerpo = await buscar(client_usuario, m, orden="relevancia")
    assert [i["codigo_bdns"] for i in cuerpo["items"]] == [en_titulo, en_finalidad]


async def test_relevancia_sin_texto_cae_a_recientes(client_usuario: AsyncClient) -> None:
    assert (await client_usuario.get("/convocatorias", params={"orden": "relevancia"})).status_code == 200


async def test_parametros_fuera_de_rango_dan_422(client_usuario: AsyncClient) -> None:
    for params in ({"size": 101}, {"q": "x" * 201}, {"orden": "azar"}, {"estado": "Inventado"}, {"nivel": "marte"}):
        assert (await client_usuario.get("/convocatorias", params=params)).status_code == 422


@pytest.mark.parametrize("campo", ["organo", "region"])
async def test_listas_de_ids_acotadas_dan_422_y_no_500(client_usuario: AsyncClient, campo: str) -> None:
    for valor in ([2**31] * 1, [-1], list(range(51)), ["x"]):
        respuesta = await client_usuario.get("/convocatorias", params={campo: valor})
        assert respuesta.status_code == 422, (campo, valor)
    assert (await client_usuario.get("/convocatorias", params={campo: [0, 2**31 - 1]})).status_code == 200


async def test_lista_de_beneficiarios_acotada(client_usuario: AsyncClient) -> None:
    assert (await client_usuario.get("/convocatorias", params={"beneficiario": ["x"] * 51})).status_code == 422
    assert (await client_usuario.get("/convocatorias", params={"beneficiario": ["x" * 201]})).status_code == 422
    assert (await client_usuario.get("/convocatorias", params={"beneficiario": ["x"] * 50})).status_code == 200


# --- Detail and summary --------------------------------------------------------


async def test_detalle_lista_documentos_sin_url_hasta_que_exista_la_ruta(client_usuario: AsyncClient) -> None:
    m = marca()
    codigo = await sembrar(
        m,
        beneficiarios=["PYME"],
        url_bases_reguladoras="https://boe.example/bases",
        detalle={
            "documentos": [
                {"id": 11, "nombre": "bases.pdf", "tamano_bytes": 10},
                {"id": 12, "nombre": "anexo.pdf", "tamano_bytes": 20},
            ],
            "instrumentos": ["Subvención"],
            "fondos": [],
            "objetivos": [],
            "regiones": ["ES12 - ASTURIAS"],
            "bases_reguladoras": "Texto",
        },
    )
    cuerpo = (await client_usuario.get(f"/convocatorias/{codigo}")).json()
    assert cuerpo["codigo_bdns"] == codigo and cuerpo["beneficiarios"] == ["PYME"]
    assert cuerpo["instrumentos"] == ["Subvención"] and cuerpo["regiones"] == ["ES12 - ASTURIAS"]
    assert all("url" not in d for d in cuerpo["documentos"])  # la ruta propia llega con el PR de documentos
    assert [d["nombre"] for d in cuerpo["documentos"]] == ["bases.pdf", "anexo.pdf"]


async def test_detalle_sin_ficha_completa_y_codigo_desconocido(client_usuario: AsyncClient) -> None:
    codigo = await sembrar(marca())  # `detalle` NULL: aún sin sincronizar
    cuerpo = (await client_usuario.get(f"/convocatorias/{codigo}")).json()
    assert cuerpo["documentos"] == [] and cuerpo["estado"] == "Plazo no especificado"
    assert (await client_usuario.get("/convocatorias/NO-EXISTE-0")).status_code == 404


async def test_resumen_cuenta_abiertas_y_suma_su_presupuesto(client_usuario: AsyncClient) -> None:
    antes = (await client_usuario.get("/convocatorias/resumen")).json()
    m = marca()
    for presupuesto in (100, 200, 300):
        await sembrar(m, fecha_fin_solicitud=HOY + timedelta(days=7), presupuesto_total=presupuesto)
    await sembrar(m, fecha_fin_solicitud=HOY - timedelta(days=7), presupuesto_total=999)
    async with AsyncSessionLocal() as db:
        db.add(SyncRun(modo="incremental", estado="ok", finished_at=datetime.now(UTC), error_detalle="TEST-resumen"))
        await db.commit()
    despues = (await client_usuario.get("/convocatorias/resumen")).json()
    assert despues["abiertas"] - antes["abiertas"] == 3
    assert despues["total"] - antes["total"] == 4
    assert despues["presupuesto_abiertas"] - antes["presupuesto_abiertas"] == 600
    assert datetime.fromisoformat(despues["ultima_sincronizacion"]) > datetime.now(UTC) - timedelta(minutes=5)


async def test_resumen_se_declara_antes_que_la_ruta_del_codigo(client_usuario: AsyncClient) -> None:
    rutas = [r.path for r in router.routes if hasattr(r, "path")]
    assert rutas.index("/convocatorias/resumen") < rutas.index("/convocatorias/{codigo_bdns}")
    cuerpo = (await client_usuario.get("/convocatorias/resumen")).json()
    assert {"abiertas", "presupuesto_abiertas", "total", "ultima_sincronizacion"} <= set(cuerpo)


# --- Fixed query count, no BDNS ------------------------------------------------


async def test_numero_de_sentencias_fijo_con_pocas_o_muchas_filas(client_usuario: AsyncClient) -> None:
    m = marca()
    await sembrar(m)
    with contar_sentencias() as pocas:
        await buscar(client_usuario, m)
    for _ in range(24):
        await sembrar(m)
    with contar_sentencias() as muchas:
        cuerpo = await buscar(client_usuario, m, size=25)
    assert len(cuerpo["items"]) == 25
    assert len(pocas) == len(muchas) == 3  # auth + count + página
    codigo = cuerpo["items"][0]["codigo_bdns"]
    with contar_sentencias() as detalle:
        await client_usuario.get(f"/convocatorias/{codigo}")
    assert len(detalle) == 2  # auth + ficha
    with contar_sentencias() as resumen:
        await client_usuario.get("/convocatorias/resumen")
    assert len(resumen) == 3  # auth + agregados + última sincronización


async def test_todo_funciona_con_la_bdns_caida(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    m = marca()
    codigo = await sembrar(m)

    async def caida(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Petición de red inesperada: {request.url}")

    # El cliente de los tests usa ASGITransport, así que esto solo corta el HTTP real hacia fuera.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", caida)
    assert await codigos(client_usuario, m) == {codigo}
    assert (await client_usuario.get(f"/convocatorias/{codigo}")).status_code == 200
    assert (await client_usuario.get("/convocatorias/resumen")).status_code == 200


# --- Authentication (spec: Authentication required) ----------------------------

RUTAS = [(r.path, sorted(r.methods or [])) for r in router.routes if hasattr(r, "path")]


def test_el_router_expone_las_rutas_de_pr4() -> None:
    assert {p for p, _ in RUTAS} >= {"/convocatorias", "/convocatorias/resumen", "/convocatorias/{codigo_bdns}"}


@pytest.mark.parametrize(("ruta", "metodos"), RUTAS)
async def test_anonimo_recibe_401_en_cada_ruta_del_router(client: AsyncClient, ruta: str, metodos: list[str]) -> None:
    assert metodos
    for metodo in metodos:
        url = ruta.replace("{codigo_bdns}", "X").replace("{id_documento}", "1")
        respuesta = await client.request(metodo, url)
        assert respuesta.status_code == 401, f"{metodo} {ruta}"


async def test_anonimo_no_consulta_la_tabla_de_convocatorias(client: AsyncClient) -> None:
    with contar_sentencias() as sentencias:
        assert (await client.get("/convocatorias", params={"q": "innovacion"})).status_code == 401
    assert not [s for s in sentencias if "convocatoria" in s]


async def test_token_invalido_da_401(client: AsyncClient) -> None:
    respuesta = await client.get("/convocatorias", headers={"Authorization": "Bearer no-es-un-token"})
    assert respuesta.status_code == 401


async def test_cuenta_bloqueada_da_403(empresa: dict) -> None:
    bloqueado: Sesion = await crear_sesion_en_bd(empresa["id"], "usuario", estado="bloqueado")
    transporte = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test", headers=bloqueado.headers) as ac:
        assert (await ac.get("/convocatorias")).status_code == 403


async def test_cualquier_rol_puede_buscar(
    client_usuario: AsyncClient, client_gestor: AsyncClient, client_admin: AsyncClient
) -> None:
    for cliente in (client_usuario, client_gestor, client_admin):
        assert (await cliente.get("/convocatorias")).status_code == 200
