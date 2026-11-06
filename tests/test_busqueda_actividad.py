"""Search filters `actividad` / `para_mi_empresa` (PR B2).

Rows are tagged with a unique title word and searched with it (shared real database).
Matching rule: the grant's CNAE sections overlap the wanted one, or the grant declares no sector
at all (open to every sector). A grant that lists sectors we could not resolve yet is NOT open.
"""

import pytest
from httpx import AsyncClient

from app.database import AsyncSessionLocal
from app.models import Empresa
from tests.test_convocatorias_api import buscar, codigos, contar_sentencias, marca, sembrar

HEADER = "x-filtro-actividad"


async def _sembrar_variedad(m: str) -> dict[str, str]:
    return {
        "C": await sembrar(m, "Industria", cnae_secciones=["C"], sectores=["Fabricación"]),
        "CJ": await sembrar(m, "Mixta", cnae_secciones=["C", "J"], sectores=["Fabricación", "Software"]),
        "J": await sembrar(m, "Software", cnae_secciones=["J"], sectores=["Software"]),
        "abierta": await sembrar(m, "Todos los sectores", cnae_secciones=[], sectores=[]),
        "sin_resolver": await sembrar(m, "Pendiente de rellenar", cnae_secciones=[], sectores=["Algo sin codigo"]),
    }


async def _actividad_de(empresa_id: int, codigo: str | None, otra: str | None = None) -> None:
    async with AsyncSessionLocal() as db:
        e = await db.get(Empresa, empresa_id)
        assert e is not None
        e.actividad_codigo, e.actividad_otra = codigo, otra
        await db.commit()


async def test_actividad_matches_overlap_and_open_grants_only(client_usuario: AsyncClient) -> None:
    m = marca()
    g = await _sembrar_variedad(m)
    assert await codigos(client_usuario, m, actividad="C") == {g["C"], g["CJ"], g["abierta"]}
    assert await codigos(client_usuario, m, actividad="J") == {g["CJ"], g["J"], g["abierta"]}
    assert await codigos(client_usuario, m, actividad="K") == {g["abierta"]}


async def test_without_the_filter_everything_is_returned(client_usuario: AsyncClient) -> None:
    m = marca()
    g = await _sembrar_variedad(m)
    assert await codigos(client_usuario, m) == set(g.values())


async def test_para_mi_empresa_uses_the_company_section(client_usuario: AsyncClient, usuario_raso) -> None:
    m = marca()
    g = await _sembrar_variedad(m)
    await _actividad_de(usuario_raso.empresa_id, "J")
    respuesta = await client_usuario.get("/convocatorias", params={"q": m, "para_mi_empresa": True})
    assert {i["codigo_bdns"] for i in respuesta.json()["items"]} == {g["CJ"], g["J"], g["abierta"]}
    assert HEADER not in respuesta.headers


@pytest.mark.parametrize(("codigo", "otra"), [(None, None), ("OTRA", "Apicultura")])
async def test_para_mi_empresa_is_ignored_for_pending_or_otra(
    client_usuario: AsyncClient, usuario_raso, codigo: str | None, otra: str | None
) -> None:
    m = marca()
    g = await _sembrar_variedad(m)
    await _actividad_de(usuario_raso.empresa_id, codigo, otra)
    respuesta = await client_usuario.get("/convocatorias", params={"q": m, "para_mi_empresa": True})
    assert {i["codigo_bdns"] for i in respuesta.json()["items"]} == set(g.values())
    assert respuesta.headers[HEADER] == "ignorado"


async def test_explicit_otra_is_ignored_and_flagged(client_usuario: AsyncClient) -> None:
    m = marca()
    g = await _sembrar_variedad(m)
    respuesta = await client_usuario.get("/convocatorias", params={"q": m, "actividad": "OTRA"})
    assert respuesta.status_code == 200
    assert {i["codigo_bdns"] for i in respuesta.json()["items"]} == set(g.values())
    assert respuesta.headers[HEADER] == "ignorado"


async def test_explicit_actividad_wins_over_para_mi_empresa(client_usuario: AsyncClient, usuario_raso) -> None:
    m = marca()
    g = await _sembrar_variedad(m)
    await _actividad_de(usuario_raso.empresa_id, "J")
    r = await client_usuario.get("/convocatorias", params={"q": m, "para_mi_empresa": True, "actividad": "C"})
    assert {i["codigo_bdns"] for i in r.json()["items"]} == {g["C"], g["CJ"], g["abierta"]}


async def test_unknown_actividad_is_422(client_usuario: AsyncClient) -> None:
    assert (await client_usuario.get("/convocatorias", params={"actividad": "Z"})).status_code == 422


async def test_total_counts_the_filtered_set(client_usuario: AsyncClient) -> None:
    m = marca()
    await _sembrar_variedad(m)
    assert (await buscar(client_usuario, m, actividad="C"))["total"] == 3


async def test_export_shares_the_filter(client_usuario: AsyncClient, usuario_raso) -> None:
    m = marca()
    g = await _sembrar_variedad(m)
    await _actividad_de(usuario_raso.empresa_id, "J")
    r = await client_usuario.get("/convocatorias/export", params={"q": m, "formato": "csv", "para_mi_empresa": True})
    assert r.status_code == 200
    assert g["J"] in r.text and g["C"] not in r.text


async def test_para_mi_empresa_costs_exactly_one_extra_query(client_usuario: AsyncClient, usuario_raso) -> None:
    m = marca()
    await _sembrar_variedad(m)
    await _actividad_de(usuario_raso.empresa_id, "J")
    with contar_sentencias() as base:
        await client_usuario.get("/convocatorias", params={"q": m})
    with contar_sentencias() as con_filtro:
        await client_usuario.get("/convocatorias", params={"q": m, "para_mi_empresa": True})
    with contar_sentencias() as explicito:
        await client_usuario.get("/convocatorias", params={"q": m, "actividad": "J"})
    assert len(con_filtro) == len(base) + 1  # the company lookup, nothing per row
    assert len(explicito) == len(base)  # an explicit section needs no lookup
