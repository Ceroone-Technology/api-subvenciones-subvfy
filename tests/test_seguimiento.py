"""Follow-up board (plan N5a/N5b): board, tasks, events, funnel and role rules."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.main import app
from app.models import Seguimiento
from tests.conftest import Sesion, codigo_bdns_de_prueba, crear_convocatoria_en_bd, crear_sesion_en_bd
from tests.migraciones import base_temporal


def _cliente(sesion: Sesion) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=sesion.headers)


async def _anadir(c: AsyncClient, **extra) -> dict:
    conv = await crear_convocatoria_en_bd(
        "Ayuda de prueba", presupuesto_total=1000, fecha_fin_solicitud=date.today() + timedelta(days=10), **extra
    )
    r = await c.post("/seguimiento", json={"codigo_bdns": conv.codigo_bdns, "titulo": "Ayuda de prueba"})
    assert r.status_code == 201, r.text
    return r.json()


async def test_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/seguimiento")).status_code == 401


async def test_add_returns_the_card_in_the_first_column(client_gestor: AsyncClient) -> None:
    item = await _anadir(client_gestor, organo_convocante="Ministerio")
    assert item["estado"] == "interesa" and item["resultado"] is None and item["responsable_id"] is None
    assert item["importe"] == 1000 and item["organo"] == "Ministerio" and item["tema"] == "otro"
    assert item["tareas"] == [] and item["tareas_hechas"] == 0 and item["tareas_total"] == 0
    assert {"id", "codigo_bdns", "titulo", "fecha_fin_solicitud", "fecha_presentacion"} <= item.keys()


async def test_add_seeds_an_uncached_call_and_keeps_a_synced_one(client_gestor: AsyncClient) -> None:
    codigo = codigo_bdns_de_prueba()
    r = await client_gestor.post(
        "/seguimiento",
        json={"codigo_bdns": codigo, "titulo": "Nueva", "organo": "Junta", "importe": 5000.5, "tema": "dig"},
    )
    assert r.status_code == 201
    assert (r.json()["titulo"], r.json()["organo"], r.json()["importe"], r.json()["tema"]) == (
        "Nueva", "Junta", 5000.5, "dig",
    )  # fmt: skip
    conv = await crear_convocatoria_en_bd("Titulo sincronizado")
    r = await client_gestor.post("/seguimiento", json={"codigo_bdns": conv.codigo_bdns, "titulo": "Otro titulo"})
    assert r.json()["titulo"] == "Titulo sincronizado"


async def test_add_twice_is_409_with_the_existing_card(client_gestor: AsyncClient) -> None:
    item = await _anadir(client_gestor)
    r = await client_gestor.post("/seguimiento", json={"codigo_bdns": item["codigo_bdns"], "titulo": "x"})
    assert r.status_code == 409
    assert r.json()["item"]["id"] == item["id"] and "ya está" in r.json()["detail"]
    assert len((await client_gestor.get("/seguimiento")).json()) == 1


async def test_two_companies_can_follow_the_same_call(client_gestor: AsyncClient, otra_empresa: dict) -> None:
    item = await _anadir(client_gestor)
    otro = await crear_sesion_en_bd(otra_empresa["id"], "gestor")
    async with _cliente(otro) as c:
        r = await c.post("/seguimiento", json={"codigo_bdns": item["codigo_bdns"], "titulo": "x"})
    assert r.status_code == 201 and r.json()["id"] != item["id"]


async def test_board_is_scoped_to_the_company_even_for_admin(
    client_gestor: AsyncClient, client_admin: AsyncClient, otra_empresa: dict
) -> None:
    item = await _anadir(client_gestor)
    otro = await crear_sesion_en_bd(otra_empresa["id"], "gestor")
    async with _cliente(otro) as c:
        assert (await c.get("/seguimiento")).json() == []
        assert (await c.get(f"/seguimiento/{item['id']}")).status_code == 404
        assert (await c.patch(f"/seguimiento/{item['id']}", json={"estado": "preparando"})).status_code == 404
        assert (await c.delete(f"/seguimiento/{item['id']}")).status_code == 404
        assert (await c.post(f"/seguimiento/{item['id']}/tareas", json={"titulo": "x"})).status_code == 404
    assert (await client_admin.get("/seguimiento")).json() == []
    assert (await client_admin.get(f"/seguimiento/{item['id']}")).status_code == 404
    assert (await client_admin.get("/seguimiento/resumen")).json()["total"] == 0


async def test_list_filters_by_state_and_person(client_gestor: AsyncClient, gestor: Sesion) -> None:
    a, b = await _anadir(client_gestor), await _anadir(client_gestor)
    await client_gestor.patch(f"/seguimiento/{a['id']}", json={"estado": "preparando", "responsable_id": gestor.id})
    assert [i["id"] for i in (await client_gestor.get("/seguimiento?estado=preparando")).json()] == [a["id"]]
    assert [i["id"] for i in (await client_gestor.get(f"/seguimiento?responsable_id={gestor.id}")).json()] == [a["id"]]
    assert len((await client_gestor.get("/seguimiento")).json()) == 2
    assert (await client_gestor.get("/seguimiento?estado=nada")).status_code == 422
    assert b["id"]


async def test_gestor_moves_assigns_and_resolves(client_gestor: AsyncClient, usuario_raso: Sesion) -> None:
    item = await _anadir(client_gestor)
    r = await client_gestor.patch(
        f"/seguimiento/{item['id']}", json={"estado": "presentada", "responsable_id": usuario_raso.id}
    )
    assert r.status_code == 200
    assert r.json()["estado"] == "presentada" and r.json()["responsable_id"] == usuario_raso.id
    assert r.json()["fecha_presentacion"] == datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()
    r = await client_gestor.patch(f"/seguimiento/{item['id']}", json={"estado": "resuelta", "resultado": "concedida"})
    assert r.json()["resultado"] == "concedida"
    # Leaving Resuelta forgets the outcome.
    r = await client_gestor.patch(f"/seguimiento/{item['id']}", json={"estado": "preparando"})
    assert r.json()["resultado"] is None
    # Unassign with an explicit null.
    r = await client_gestor.patch(f"/seguimiento/{item['id']}", json={"responsable_id": None})
    assert r.json()["responsable_id"] is None


async def test_resultado_only_in_resuelta_and_invalid_values(client_gestor: AsyncClient) -> None:
    item = await _anadir(client_gestor)
    url = f"/seguimiento/{item['id']}"
    assert (await client_gestor.patch(url, json={"resultado": "concedida"})).status_code == 422
    assert (await client_gestor.patch(url, json={"estado": "x"})).status_code == 422
    assert (await client_gestor.patch(url, json={"estado": None})).status_code == 422
    assert (await client_gestor.patch(url, json={"resultado": "quizas", "estado": "resuelta"})).status_code == 422


async def test_responsable_must_be_an_active_user_of_the_company(
    client_gestor: AsyncClient, otra_empresa: dict, empresa: dict
) -> None:
    item = await _anadir(client_gestor)
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "usuario")
    bloqueado = await crear_sesion_en_bd(empresa["id"], "usuario", estado="bloqueado")
    for uid in (ajeno.id, bloqueado.id, 999_999_999):
        r = await client_gestor.patch(f"/seguimiento/{item['id']}", json={"responsable_id": uid})
        assert r.status_code == 400, r.text


async def test_usuario_cannot_move_an_unassigned_card_but_can_take_and_move_it(
    client_gestor: AsyncClient, client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    item = await _anadir(client_gestor)
    url = f"/seguimiento/{item['id']}"
    assert len((await client_usuario.get("/seguimiento")).json()) == 1  # sees the whole board
    assert (await client_usuario.patch(url, json={"estado": "preparando"})).status_code == 403
    assert (await client_usuario.patch(url, json={"responsable_id": 12345})).status_code == 403
    r = await client_usuario.patch(url, json={"responsable_id": usuario_raso.id})
    assert r.status_code == 200 and r.json()["responsable_id"] == usuario_raso.id
    assert (await client_usuario.patch(url, json={"estado": "preparando"})).json()["estado"] == "preparando"
    # Taking and moving in one request also works on another unassigned card.
    otra = await _anadir(client_gestor)
    r = await client_usuario.patch(
        f"/seguimiento/{otra['id']}", json={"responsable_id": usuario_raso.id, "estado": "preparando"}
    )
    assert r.status_code == 200 and r.json()["estado"] == "preparando"


async def test_usuario_cannot_touch_cards_of_others_resolve_or_delete(
    client_gestor: AsyncClient, client_usuario: AsyncClient, gestor: Sesion, usuario_raso: Sesion
) -> None:
    item = await _anadir(client_gestor)
    url = f"/seguimiento/{item['id']}"
    await client_gestor.patch(url, json={"responsable_id": gestor.id})
    assert (await client_usuario.patch(url, json={"estado": "preparando"})).status_code == 403
    assert (await client_usuario.patch(url, json={"responsable_id": usuario_raso.id})).status_code == 403  # not free
    assert (await client_usuario.post(f"{url}/tareas", json={"titulo": "x"})).status_code == 403
    await client_gestor.patch(url, json={"responsable_id": usuario_raso.id, "estado": "resuelta"})
    assert (await client_usuario.patch(url, json={"resultado": "concedida"})).status_code == 403
    assert (await client_usuario.patch(url, json={"fecha_presentacion": "2026-01-01"})).status_code == 403
    assert (await client_usuario.delete(url)).status_code == 403
    assert (await client_gestor.delete(url)).status_code == 204
    assert (await client_gestor.get(url)).status_code == 404


async def test_tasks_crud_and_counters(client_gestor: AsyncClient) -> None:
    item = await _anadir(client_gestor)
    url = f"/seguimiento/{item['id']}/tareas"
    cuerpo = {"titulo": "  Memoria técnica ", "tipo": "documento", "fecha_limite": "2026-12-01"}
    r = await client_gestor.post(url, json=cuerpo)
    assert r.status_code == 201
    assert r.json()["tareas"][0]["titulo"] == "Memoria técnica" and r.json()["tareas"][0]["tipo"] == "documento"
    r = await client_gestor.post(url, json={"titulo": "Llamar a la gestoría"})
    assert r.json()["tareas_total"] == 2 and r.json()["tareas"][1]["tipo"] == "tarea"
    tarea_id = r.json()["tareas"][0]["id"]
    r = await client_gestor.patch(f"{url}/{tarea_id}", json={"hecha": True})
    assert (r.json()["tareas_hechas"], r.json()["tareas_total"]) == (1, 2)
    r = await client_gestor.patch(f"{url}/{tarea_id}", json={"titulo": "Memoria", "fecha_limite": None})
    assert r.json()["tareas"][0]["titulo"] == "Memoria" and r.json()["tareas"][0]["fecha_limite"] is None
    r = await client_gestor.delete(f"{url}/{tarea_id}")
    assert r.status_code == 200 and r.json()["tareas_total"] == 1 and r.json()["tareas_hechas"] == 0
    assert (await client_gestor.patch(f"{url}/{tarea_id}", json={"hecha": True})).status_code == 404


@pytest.mark.parametrize("cuerpo", [{"titulo": ""}, {"titulo": "   "}, {"titulo": "x", "tipo": "otro"}, {}])
async def test_task_validation(client_gestor: AsyncClient, cuerpo: dict) -> None:
    item = await _anadir(client_gestor)
    assert (await client_gestor.post(f"/seguimiento/{item['id']}/tareas", json=cuerpo)).status_code == 422


async def test_a_task_of_another_card_is_a_404(client_gestor: AsyncClient) -> None:
    a, b = await _anadir(client_gestor), await _anadir(client_gestor)
    tarea = (await client_gestor.post(f"/seguimiento/{a['id']}/tareas", json={"titulo": "x"})).json()["tareas"][0]
    r = await client_gestor.patch(f"/seguimiento/{b['id']}/tareas/{tarea['id']}", json={"hecha": True})
    assert r.status_code == 404


async def test_usuario_edits_tasks_only_on_own_cards_and_assigns_only_to_self(
    client_gestor: AsyncClient, client_usuario: AsyncClient, usuario_raso: Sesion, gestor: Sesion
) -> None:
    item = await _anadir(client_gestor)
    await client_gestor.patch(f"/seguimiento/{item['id']}", json={"responsable_id": usuario_raso.id})
    url = f"/seguimiento/{item['id']}/tareas"
    assert (await client_usuario.post(url, json={"titulo": "mía", "asignado_a": gestor.id})).status_code == 403
    r = await client_usuario.post(url, json={"titulo": "mía", "asignado_a": usuario_raso.id})
    assert r.status_code == 201 and r.json()["tareas"][0]["asignado_a"] == usuario_raso.id
    assert (await client_gestor.post(url, json={"titulo": "x", "asignado_a": 999_999_999})).status_code == 400


async def test_events_log_the_movements(client_gestor: AsyncClient, gestor: Sesion) -> None:
    item = await _anadir(client_gestor)
    url = f"/seguimiento/{item['id']}"
    await client_gestor.patch(url, json={"estado": "preparando", "responsable_id": gestor.id})
    await client_gestor.patch(url, json={"estado": "resuelta", "resultado": "denegada"})
    await client_gestor.patch(url, json={"estado": "resuelta"})  # no change: no event
    eventos = (await client_gestor.get(f"{url}/eventos")).json()
    tipos = [(e["tipo"], e["estado_anterior"], e["estado_nuevo"]) for e in eventos]
    assert sorted(tipos, key=str) == sorted(
        [("creada", None, "interesa"), ("movida", "interesa", "preparando"), ("asignada", None, None),
         ("movida", "preparando", "resuelta"), ("resuelta", None, "denegada")], key=str,
    )  # fmt: skip
    assert all(e["usuario_id"] == gestor.id for e in eventos)


async def test_summary_counts_and_sums_per_column(client_gestor: AsyncClient) -> None:
    vacio = (await client_gestor.get("/seguimiento/resumen")).json()
    assert [e["estado"] for e in vacio["estados"]] == ["interesa", "preparando", "presentada", "resuelta"]
    assert vacio["total"] == 0 and vacio["concedidas"] == {"total": 0, "importe": 0}
    a, b, c = await _anadir(client_gestor), await _anadir(client_gestor), await _anadir(client_gestor)
    await client_gestor.patch(f"/seguimiento/{b['id']}", json={"estado": "resuelta", "resultado": "concedida"})
    await client_gestor.patch(f"/seguimiento/{c['id']}", json={"estado": "resuelta", "resultado": "denegada"})
    r = (await client_gestor.get("/seguimiento/resumen")).json()
    por = {e["estado"]: e for e in r["estados"]}
    assert (por["interesa"]["total"], por["interesa"]["importe"]) == (1, 1000)
    assert (por["resuelta"]["total"], por["resuelta"]["importe"]) == (2, 2000)
    assert r["concedidas"] == {"total": 1, "importe": 1000} and r["total"] == 3 and r["importe_total"] == 3000
    assert a["id"]


async def test_db_checks_reject_inconsistent_rows(client_gestor: AsyncClient) -> None:
    item = await _anadir(client_gestor)
    async with AsyncSessionLocal() as db:
        seg = (await db.execute(select(Seguimiento).where(Seguimiento.id == item["id"]))).scalar_one()
        seg.resultado = "concedida"  # still in `interesa`
        with pytest.raises(IntegrityError, match="ck_seguimiento_resultado_resuelta"):
            await db.commit()
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="ck_seguimiento_estado"):
            await db.execute(text("UPDATE seguimiento SET estado = 'otro' WHERE id = :i"), {"i": item["id"]})


async def test_board_listing_uses_a_fixed_number_of_queries(client_gestor: AsyncClient) -> None:
    from sqlalchemy import event

    from app.database import engine

    for _ in range(6):
        item = await _anadir(client_gestor)
        await client_gestor.post(f"/seguimiento/{item['id']}/tareas", json={"titulo": "t"})
    consultas: list[str] = []
    escuchar = lambda conn, cur, stmt, *a: consultas.append(stmt)  # noqa: E731
    event.listen(engine.sync_engine, "before_cursor_execute", escuchar)
    try:
        assert len((await client_gestor.get("/seguimiento")).json()) == 6
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", escuchar)
    assert len(consultas) <= 3  # auth + cards + tasks


async def test_migration_up_and_down() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", "head")
        async with base.motor.connect() as c:
            tablas = {
                f[0]
                for f in await c.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'seguimiento%'"))
            }
            assert tablas == {"seguimiento", "seguimiento_tarea", "seguimiento_evento"}
        base.alembic("downgrade", "f7a3c9d15e82")
        async with base.motor.connect() as c:
            assert not (await c.execute(text("SELECT 1 FROM pg_tables WHERE tablename LIKE 'seguimiento%'"))).first()
