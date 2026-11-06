"""Favorites card fields, filters and summary (plan N7)."""

from datetime import date, timedelta

import pytest
from httpx import AsyncClient

from tests.conftest import Sesion, crear_convocatoria_en_bd, crear_sesion_en_bd


def _en(dias: int) -> date:
    return date.today() + timedelta(days=dias)


async def _fav(c: AsyncClient, titulo: str, *, fin: date | None = None, nota: str | None = None, **extra) -> dict:
    conv = await crear_convocatoria_en_bd(titulo, fecha_fin_solicitud=fin, **extra)
    cuerpo = {"convocatoria": {"codigo_bdns": conv.codigo_bdns, "titulo": titulo}, "nota": nota}
    r = await c.post("/favoritos", json=cuerpo)
    assert r.status_code == 201, r.text
    return r.json()


async def test_card_fields_come_with_the_favorite(client_gestor: AsyncClient) -> None:
    creado = await _fav(
        client_gestor, "Con plazo", fin=_en(5), fecha_inicio_solicitud=_en(-10), presupuesto_total=2500000, tema="dig"
    )
    c = creado["convocatoria"]
    assert c["fecha_fin_solicitud"] == _en(5).isoformat() and c["fecha_inicio_solicitud"] == _en(-10).isoformat()
    assert c["estado"] == "Abierta" and c["dias_restantes"] == 5
    assert c["presupuesto_total"] == 2500000 and c["tema"] == "dig" and c["gestiona_subvfy"] is False
    assert {"plazo_permanente", "codigo_bdns", "titulo"} <= c.keys()
    # The same shape in the list, the detail and after editing the note.
    lista = (await client_gestor.get("/favoritos")).json()["items"][0]["convocatoria"]
    detalle = (await client_gestor.get(f"/favoritos/{c['codigo_bdns']}")).json()["convocatoria"]
    editado = (await client_gestor.patch(f"/favoritos/{c['codigo_bdns']}", json={"nota": "x"})).json()["convocatoria"]
    assert lista == detalle == editado == c


async def test_state_and_days_for_closed_and_undated_calls(client_gestor: AsyncClient) -> None:
    cerrada = await _fav(client_gestor, "Cerrada", fin=_en(-3))
    assert cerrada["convocatoria"]["estado"] == "Cerrada" and cerrada["convocatoria"]["dias_restantes"] == -3
    sin = await _fav(client_gestor, "Sin fecha")
    assert sin["convocatoria"]["dias_restantes"] is None and sin["convocatoria"]["fecha_fin_solicitud"] is None


async def test_filter_closing_within_days_is_inclusive_and_skips_closed(client_gestor: AsyncClient) -> None:
    await _fav(client_gestor, "Hoy", fin=_en(0))
    await _fav(client_gestor, "En 14", fin=_en(14))
    await _fav(client_gestor, "En 15", fin=_en(15))
    await _fav(client_gestor, "Ayer", fin=_en(-1))
    await _fav(client_gestor, "Sin fecha")
    r = await client_gestor.get("/favoritos", params={"cierran_en_dias": 14})
    assert {i["convocatoria"]["titulo"] for i in r.json()["items"]} == {"Hoy", "En 14"}
    assert r.json()["total"] == 2
    assert (await client_gestor.get("/favoritos", params={"cierran_en_dias": 0})).json()["total"] == 1
    assert (await client_gestor.get("/favoritos", params={"cierran_en_dias": -1})).status_code == 422
    assert (await client_gestor.get("/favoritos", params={"cierran_en_dias": 366})).status_code == 422


async def test_filter_with_and_without_note_ignores_blank_notes(client_gestor: AsyncClient) -> None:
    await _fav(client_gestor, "Con nota", nota="importante")
    await _fav(client_gestor, "Nota vacía", nota="   ")
    await _fav(client_gestor, "Sin nota")
    con = (await client_gestor.get("/favoritos", params={"con_nota": "true"})).json()
    sin = (await client_gestor.get("/favoritos", params={"con_nota": "false"})).json()
    assert [i["convocatoria"]["titulo"] for i in con["items"]] == ["Con nota"]
    assert {i["convocatoria"]["titulo"] for i in sin["items"]} == {"Nota vacía", "Sin nota"}
    assert (await client_gestor.get("/favoritos")).json()["total"] == 3
    # Filters combine with the search and paginate over the filtered total.
    combinado = await client_gestor.get("/favoritos", params={"con_nota": "false", "q": "Sin", "size": 1})
    assert combinado.json()["total"] == 1 and len(combinado.json()["items"]) == 1


async def test_summary_of_an_empty_list(client_gestor: AsyncClient) -> None:
    r = await client_gestor.get("/favoritos/resumen")
    assert r.status_code == 200
    assert r.json() == {
        "total": 0, "importe_total": 0, "cierran_en_14_dias": 0, "con_nota": 0,
        "por_plazo": {"vencidas": 0, "hasta_7_dias": 0, "de_8_a_30_dias": 0, "mas_de_30_dias": 0, "sin_fecha": 0},
    }  # fmt: skip


async def test_summary_counts_sums_and_buckets(client_gestor: AsyncClient) -> None:
    await _fav(client_gestor, "A", fin=_en(-2), presupuesto_total=100, nota="n")
    await _fav(client_gestor, "B", fin=_en(0), presupuesto_total=200)
    await _fav(client_gestor, "C", fin=_en(7), presupuesto_total=300, nota=" ")
    await _fav(client_gestor, "D", fin=_en(8), presupuesto_total=400)
    await _fav(client_gestor, "E", fin=_en(14))
    await _fav(client_gestor, "F", fin=_en(30), nota="otra")
    await _fav(client_gestor, "G", fin=_en(31))
    await _fav(client_gestor, "H")
    r = (await client_gestor.get("/favoritos/resumen")).json()
    assert r["total"] == 8 and r["importe_total"] == 1000
    assert r["cierran_en_14_dias"] == 4  # B, C, D, E (the closed one and the far ones do not count)
    assert r["con_nota"] == 2
    p = r["por_plazo"]
    assert p == {"vencidas": 1, "hasta_7_dias": 2, "de_8_a_30_dias": 3, "mas_de_30_dias": 1, "sin_fecha": 1}
    assert sum(p.values()) == r["total"]


async def test_summary_is_personal(client_gestor: AsyncClient, gestor: Sesion) -> None:
    await _fav(client_gestor, "Mía", fin=_en(3), presupuesto_total=50)
    otro = await crear_sesion_en_bd(gestor.empresa_id, "usuario")
    from httpx import ASGITransport

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=otro.headers) as c:
        assert (await c.get("/favoritos/resumen")).json()["total"] == 0
        assert (await c.get("/favoritos", params={"cierran_en_dias": 30})).json()["total"] == 0


async def test_summary_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/favoritos/resumen")).status_code == 401


@pytest.mark.parametrize("codigo", ["resumen"])
async def test_summary_route_does_not_clash_with_the_code_route(client_gestor: AsyncClient, codigo: str) -> None:
    # `/favoritos/{codigo_bdns}` would answer 404 for an unknown code; /resumen must win.
    assert (await client_gestor.get(f"/favoritos/{codigo}")).status_code == 200
