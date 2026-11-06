"""Alerts summary and history fields (plan N8)."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Alerta, AlertaEjecucion
from app.services.alertas_resumen import EJECUCIONES_POR_ALERTA, calcular_proxima_ejecucion
from tests.conftest import Sesion, crear_alerta_en_bd, crear_sesion_en_bd

AHORA = datetime(2026, 11, 10, 10, 7, 30, tzinfo=UTC)


async def _ejecucion(
    alerta_id: int, usuario_id: int, hace_horas: float, encontradas: int, estado: str = "enviado"
) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            AlertaEjecucion(
                alerta_id=alerta_id,
                fecha_ejecucion_at=datetime.now(UTC) - timedelta(hours=hace_horas),
                convocatorias_encontradas=encontradas,
                estado_envio=estado,
                detalle_error="boom" if estado == "error" else None,
                created_by=usuario_id,
                updated_by=usuario_id,
            )
        )
        await db.commit()


def _alerta(**campos) -> Alerta:
    base = {"activa": True, "frecuencia": "diaria", "ultima_ejecucion_at": None, "proximo_reintento_at": None}
    return Alerta(**{**base, **campos})


# --- pure rule ---------------------------------------------------------------------------------------------


def test_paused_alert_has_no_next_run() -> None:
    assert calcular_proxima_ejecucion(_alerta(activa=False), AHORA) is None


def test_never_run_alert_goes_on_the_next_cycle() -> None:
    proxima = calcular_proxima_ejecucion(_alerta(), AHORA)
    assert proxima is not None and proxima > AHORA
    assert proxima - AHORA <= timedelta(minutes=settings.scheduler_intervalo_minutos)
    assert proxima.timestamp() % (settings.scheduler_intervalo_minutos * 60) == 0


@pytest.mark.parametrize(("frecuencia", "dias"), [("diaria", 1), ("semanal", 7)])
def test_next_run_is_last_run_plus_interval_rounded_to_a_cycle(frecuencia: str, dias: int) -> None:
    ultima = AHORA - timedelta(hours=1)
    proxima = calcular_proxima_ejecucion(_alerta(frecuencia=frecuencia, ultima_ejecucion_at=ultima), AHORA)
    esperado = ultima + timedelta(days=dias)
    paso = settings.scheduler_intervalo_minutos * 60
    assert proxima is not None and esperado <= proxima < esperado + timedelta(seconds=paso)


def test_overdue_alert_is_not_in_the_past() -> None:
    vieja = _alerta(frecuencia="diaria", ultima_ejecucion_at=AHORA - timedelta(days=3))
    proxima = calcular_proxima_ejecucion(vieja, AHORA)
    assert proxima is not None and AHORA < proxima <= AHORA + timedelta(minutes=settings.scheduler_intervalo_minutos)


def test_immediate_alert_runs_on_the_next_cycle() -> None:
    inmediata = _alerta(frecuencia="inmediata", ultima_ejecucion_at=AHORA - timedelta(minutes=1))
    proxima = calcular_proxima_ejecucion(inmediata, AHORA)
    assert proxima is not None and AHORA < proxima <= AHORA + timedelta(minutes=settings.scheduler_intervalo_minutos)


def test_failure_wait_postpones_the_run() -> None:
    espera = AHORA + timedelta(hours=2)
    ultima = AHORA - timedelta(days=1)
    fallida = _alerta(frecuencia="inmediata", ultima_ejecucion_at=ultima, proximo_reintento_at=espera)
    proxima = calcular_proxima_ejecucion(fallida, AHORA)
    paso = settings.scheduler_intervalo_minutos * 60
    assert proxima is not None and espera <= proxima < espera + timedelta(seconds=paso)
    assert math.isclose(proxima.timestamp() % paso, 0)


# --- history fields on the alert ---------------------------------------------------------------------------


async def test_alert_without_runs_has_empty_history(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id)
    a = (await client_gestor.get(f"/alertas/{alerta_id}")).json()
    assert a["historial"] == [] and a["nuevas_ultima"] == 0
    assert datetime.fromisoformat(a["proxima_ejecucion_at"]) > datetime.now(UTC)


async def test_history_is_the_last_eight_runs_newest_first(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id)
    for i in range(10):  # run 0 is the oldest (10 h ago), run 9 the newest
        await _ejecucion(alerta_id, gestor.id, hace_horas=10 - i, encontradas=i)
    a = (await client_gestor.get(f"/alertas/{alerta_id}")).json()
    assert [e["convocatorias_encontradas"] for e in a["historial"]] == [9, 8, 7, 6, 5, 4, 3, 2]
    assert len(a["historial"]) == EJECUCIONES_POR_ALERTA and a["nuevas_ultima"] == 9
    esperados = {"id", "alerta_id", "fecha_ejecucion_at", "estado_envio", "detalle_error", "created_at"}
    assert esperados <= a["historial"][0].keys()


async def test_history_is_in_the_list_without_mixing_alerts(client_gestor: AsyncClient, gestor: Sesion) -> None:
    a1, a2 = await crear_alerta_en_bd(gestor.id, nombre="A1"), await crear_alerta_en_bd(gestor.id, nombre="A2")
    await _ejecucion(a1, gestor.id, 3, 4)
    await _ejecucion(a1, gestor.id, 2, 5, "error")
    await _ejecucion(a2, gestor.id, 1, 9)
    por_nombre = {i["nombre"]: i for i in (await client_gestor.get("/alertas")).json()["items"]}
    assert [e["convocatorias_encontradas"] for e in por_nombre["A1"]["historial"]] == [5, 4]
    assert por_nombre["A1"]["nuevas_ultima"] == 5 and por_nombre["A2"]["nuevas_ultima"] == 9
    assert all(e["alerta_id"] == a2 for e in por_nombre["A2"]["historial"])


async def test_paused_alert_has_no_next_run_in_the_api(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id, activa=False)
    assert (await client_gestor.get(f"/alertas/{alerta_id}")).json()["proxima_ejecucion_at"] is None


# --- /alertas/resumen --------------------------------------------------------------------------------------


async def test_summary_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/alertas/resumen")).status_code == 401


async def test_summary_without_alerts(client_gestor: AsyncClient) -> None:
    r = await client_gestor.get("/alertas/resumen")
    assert r.status_code == 200
    assert r.json() == {
        "activas": 0, "total": 0, "novedades_semana": 0, "requieren_atencion": 0, "proxima_ejecucion_at": None,
    }  # fmt: skip


async def test_summary_figures(client_gestor: AsyncClient, gestor: Sesion) -> None:
    sana = await crear_alerta_en_bd(gestor.id, nombre="sana")
    rota = await crear_alerta_en_bd(gestor.id, nombre="rota")
    pausada = await crear_alerta_en_bd(gestor.id, nombre="pausada", activa=False)
    recuperada = await crear_alerta_en_bd(gestor.id, nombre="recuperada")
    await _ejecucion(sana, gestor.id, 2, 3)
    await _ejecucion(sana, gestor.id, 30, 2)
    await _ejecucion(sana, gestor.id, 24 * 9, 50)  # older than a week: not counted
    await _ejecucion(rota, gestor.id, 5, 0, "error")
    await _ejecucion(pausada, gestor.id, 5, 0, "error")  # paused: does not ask for attention
    await _ejecucion(recuperada, gestor.id, 20, 0, "error")
    await _ejecucion(recuperada, gestor.id, 2, 1)  # the latest run is fine
    r = (await client_gestor.get("/alertas/resumen")).json()
    assert (r["activas"], r["total"]) == (3, 4)
    assert r["novedades_semana"] == 3 + 2 + 1
    assert r["requieren_atencion"] == 1
    assert datetime.fromisoformat(r["proxima_ejecucion_at"]) > datetime.now(UTC)


async def test_summary_is_personal_and_next_run_ignores_paused(
    client_gestor: AsyncClient, gestor: Sesion, otra_empresa: dict
) -> None:
    await crear_alerta_en_bd(gestor.id, activa=False)
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "gestor")
    await crear_alerta_en_bd(ajeno.id)
    r = (await client_gestor.get("/alertas/resumen")).json()
    assert (r["activas"], r["total"], r["proxima_ejecucion_at"]) == (0, 1, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=ajeno.headers) as c:
        assert (await c.get("/alertas/resumen")).json()["total"] == 1


async def test_resumen_route_does_not_clash_with_the_id_route(client_gestor: AsyncClient) -> None:
    assert (await client_gestor.get("/alertas/resumen")).status_code == 200
    assert (await client_gestor.get("/alertas/0")).status_code == 404
