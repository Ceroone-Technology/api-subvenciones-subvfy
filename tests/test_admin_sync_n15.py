"""Admin sync: forced run, daily series and totals (plan N15).

The database is shared and holds real runs and grants, so the series and the totals are tested as **deltas** over a
baseline, and every row a test adds is marked (`TEST-` in `error_detalle`, `TST` codes) so the teardown removes it.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.api.routes import admin_sync
from app.database import AsyncSessionLocal
from app.models import SyncRun
from app.services.sync_bdns import ResultadoSync
from tests.conftest import crear_convocatoria_en_bd

HOY = date.today()


@pytest.fixture(autouse=True)
def _sin_tarea_previa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_sync, "_tarea_forzada", None)
    # A real `running` run in the shared database must not turn a 202 test into a 409.
    monkeypatch.setattr(admin_sync, "ahora", lambda: datetime.now(UTC) + timedelta(days=3))


async def _run(estado: str = "ok", *, hace_dias: int = 0, **cifras: int) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            SyncRun(
                modo="incremental", estado=estado, started_at=datetime.now(UTC) - timedelta(days=hace_dias),
                finished_at=datetime.now(UTC), error_detalle="TEST-n15", **cifras,
            )  # fmt: skip
        )
        await db.commit()


# --- roles ---------------------------------------------------------------------------------------------------


RUTAS = [("post", "/admin/sync/forzar"), ("get", "/admin/sync/serie"), ("get", "/admin/sync/totales")]


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS)
async def test_only_admin(
    client: AsyncClient, client_gestor: AsyncClient, client_usuario: AsyncClient, metodo: str, ruta: str
) -> None:
    assert (await getattr(client, metodo)(ruta)).status_code == 401
    assert (await getattr(client_gestor, metodo)(ruta)).status_code == 403
    assert (await getattr(client_usuario, metodo)(ruta)).status_code == 403


# --- forced run ----------------------------------------------------------------------------------------------


async def test_force_answers_202_and_starts_the_sync(
    client_admin: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    llamadas: list[int] = []

    async def falso() -> ResultadoSync:
        llamadas.append(1)
        return ResultadoSync(1, "ok")

    monkeypatch.setattr(admin_sync, "ejecutar_sync", falso)
    r = await client_admin.post("/admin/sync/forzar")
    assert r.status_code == 202 and r.json()["estado"] == "aceptada"
    assert admin_sync._tarea_forzada is not None
    await admin_sync._tarea_forzada
    assert llamadas == [1]


async def test_a_second_click_while_running_is_409(client_admin: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    suelta = asyncio.Event()

    async def lento() -> ResultadoSync:
        await suelta.wait()
        return ResultadoSync(1, "ok")

    monkeypatch.setattr(admin_sync, "ejecutar_sync", lento)
    assert (await client_admin.post("/admin/sync/forzar")).status_code == 202
    segundo = await client_admin.post("/admin/sync/forzar")
    assert segundo.status_code == 409 and "en curso" in segundo.json()["detail"]
    suelta.set()
    assert admin_sync._tarea_forzada is not None
    await admin_sync._tarea_forzada
    monkeypatch.setattr(admin_sync, "ejecutar_sync", lambda: _ok())
    assert (await client_admin.post("/admin/sync/forzar")).status_code == 202  # free again
    await admin_sync._tarea_forzada


async def _ok() -> ResultadoSync:
    return ResultadoSync(1, "ok")


async def test_a_running_run_in_the_database_is_409(client_admin: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_sync, "ahora", lambda: datetime.now(UTC))
    await _run("running")
    r = await client_admin.post("/admin/sync/forzar")
    assert r.status_code == 409


async def test_a_failure_in_the_background_does_not_break_the_server(
    client_admin: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def roto() -> ResultadoSync:
        raise RuntimeError("boom")

    monkeypatch.setattr(admin_sync, "ejecutar_sync", roto)
    assert (await client_admin.post("/admin/sync/forzar")).status_code == 202
    assert admin_sync._tarea_forzada is not None
    await admin_sync._tarea_forzada  # the exception is logged inside, never raised


async def test_the_advisory_lock_answer_is_handled(client_admin: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def bloqueado() -> None:
        return None

    monkeypatch.setattr(admin_sync, "ejecutar_sync", bloqueado)
    assert (await client_admin.post("/admin/sync/forzar")).status_code == 202
    assert admin_sync._tarea_forzada is not None
    await admin_sync._tarea_forzada


# --- series --------------------------------------------------------------------------------------------------


async def test_series_has_one_point_per_day_oldest_first(client_admin: AsyncClient) -> None:
    r = await client_admin.get("/admin/sync/serie")
    puntos = r.json()
    assert r.status_code == 200 and len(puntos) == 14
    fechas = [date.fromisoformat(p["fecha"]) for p in puntos]
    assert fechas == sorted(fechas) and len(set(fechas)) == 14
    esperados = {"fecha", "ejecuciones", "fallidas", "insertadas", "actualizadas", "retiradas", "errores"}
    assert esperados == puntos[0].keys()
    assert len((await client_admin.get("/admin/sync/serie", params={"dias": 3})).json()) == 3


@pytest.mark.parametrize("dias", [0, 91, -1])
async def test_series_days_are_validated(client_admin: AsyncClient, dias: int) -> None:
    assert (await client_admin.get("/admin/sync/serie", params={"dias": dias})).status_code == 422


async def test_series_adds_up_the_runs_of_a_day(client_admin: AsyncClient) -> None:
    antes = {p["fecha"]: p for p in (await client_admin.get("/admin/sync/serie", params={"dias": 5})).json()}
    await _run("ok", insertadas=10, actualizadas=2, retiradas=1, errores=0)
    await _run("failed", insertadas=3, errores=4)
    await _run("ok", hace_dias=2, insertadas=7)
    despues = {p["fecha"]: p for p in (await client_admin.get("/admin/sync/serie", params={"dias": 5})).json()}
    hoy, antier = despues[HOY.isoformat()], despues[(HOY - timedelta(days=2)).isoformat()]
    b_hoy, b_antier = antes[HOY.isoformat()], antes[(HOY - timedelta(days=2)).isoformat()]
    assert hoy["ejecuciones"] - b_hoy["ejecuciones"] == 2 and hoy["fallidas"] - b_hoy["fallidas"] == 1
    assert hoy["insertadas"] - b_hoy["insertadas"] == 13 and hoy["actualizadas"] - b_hoy["actualizadas"] == 2
    assert hoy["retiradas"] - b_hoy["retiradas"] == 1 and hoy["errores"] - b_hoy["errores"] == 4
    assert antier["ejecuciones"] - b_antier["ejecuciones"] == 1 and antier["insertadas"] - b_antier["insertadas"] == 7


async def test_series_ignores_runs_outside_the_window(client_admin: AsyncClient) -> None:
    antes = sum(p["ejecuciones"] for p in (await client_admin.get("/admin/sync/serie", params={"dias": 3})).json())
    await _run("ok", hace_dias=10)
    despues = sum(p["ejecuciones"] for p in (await client_admin.get("/admin/sync/serie", params={"dias": 3})).json())
    assert despues == antes


# --- totals --------------------------------------------------------------------------------------------------


async def test_totals_shape_and_consistency(client_admin: AsyncClient) -> None:
    r = await client_admin.get("/admin/sync/totales")
    t = r.json()
    assert r.status_code == 200 and t.keys() == {
        "en_base_de_datos", "abiertas", "fallos_14_dias", "ultima_sincronizacion_at"
    }  # fmt: skip
    assert 0 <= t["abiertas"] <= t["en_base_de_datos"]


async def test_totals_count_grants_open_grants_and_failures(client_admin: AsyncClient) -> None:
    antes = (await client_admin.get("/admin/sync/totales")).json()
    manana = HOY + timedelta(days=1)
    hace5 = HOY - timedelta(days=5)
    await crear_convocatoria_en_bd("abierta", fecha_inicio_solicitud=hace5, fecha_fin_solicitud=manana)
    await crear_convocatoria_en_bd("cerrada", fecha_fin_solicitud=HOY - timedelta(days=5))
    await crear_convocatoria_en_bd("sin plazo", plazo_permanente=True)
    await crear_convocatoria_en_bd(
        "retirada", fecha_fin_solicitud=manana, removed_at=datetime.now(UTC)
    )  # removed rows count nowhere
    await _run("failed")
    await _run("failed", hace_dias=20)  # outside the 14 days
    await _run("ok")
    despues = (await client_admin.get("/admin/sync/totales")).json()
    assert despues["en_base_de_datos"] - antes["en_base_de_datos"] == 3
    assert despues["abiertas"] - antes["abiertas"] == 2
    assert despues["fallos_14_dias"] - antes["fallos_14_dias"] == 1
    assert despues["ultima_sincronizacion_at"] is not None
