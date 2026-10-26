"""Scheduler flags, CLI and health endpoint of the BDNS sync (PR3, tasks 3.5-3.7)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app import cli
from app.api.routes import admin_sync
from app.config import settings
from app.core.scheduler import JOB_MOTOR_ALERTAS, JOB_SYNC_BDNS, iniciar_scheduler, parar_scheduler
from app.database import AsyncSessionLocal
from app.models import SyncRun
from app.services.sync_bdns import MOTIVO_PRESUPUESTO, ResultadoSync
from tests.conftest import Sesion


@pytest.mark.parametrize(
    ("alertas", "sync", "esperados"),
    [
        (True, False, {JOB_MOTOR_ALERTAS}),
        (False, True, {JOB_SYNC_BDNS}),
        (True, True, {JOB_MOTOR_ALERTAS, JOB_SYNC_BDNS}),
        (False, False, set()),
    ],
)
async def test_cada_flag_arranca_solo_su_job(
    monkeypatch: pytest.MonkeyPatch, alertas: bool, sync: bool, esperados: set[str]
) -> None:
    monkeypatch.setattr(settings, "scheduler_habilitado", alertas)
    monkeypatch.setattr(settings, "sync_habilitado", sync)
    scheduler = iniciar_scheduler()
    try:
        assert (set() if scheduler is None else {j.id for j in scheduler.get_jobs()}) == esperados
        if sync and scheduler is not None:
            trigger = scheduler.get_job(JOB_SYNC_BDNS).trigger  # type: ignore[union-attr]
            assert "hour='7,13,19'" in str(trigger) and str(trigger.timezone) == "Europe/Madrid"
    finally:
        await parar_scheduler(scheduler)


def test_cli_sync_bdns_pasa_los_argumentos_sin_pedir_password(monkeypatch: pytest.MonkeyPatch) -> None:
    falso = AsyncMock(return_value=ResultadoSync(1, "ok"))
    monkeypatch.setattr(cli, "ejecutar_sync", falso)
    cli.main(["sync-bdns", "--backfill", "--meses", "6", "--max-minutos", "5"])
    falso.assert_awaited_once_with(modo="backfill", meses=6, max_minutos=5)
    falso.reset_mock()
    cli.main(["sync-bdns"])
    falso.assert_awaited_once_with(modo="incremental", meses=None, max_minutos=None)


def test_cli_sync_bdns_falla_si_el_run_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "ejecutar_sync", AsyncMock(return_value=ResultadoSync(1, "failed")))
    with pytest.raises(SystemExit) as salida:
        cli.main(["sync-bdns"])
    assert salida.value.code == 1


async def _run(estado: str, finished: datetime) -> None:
    async with AsyncSessionLocal() as db:
        db.add(SyncRun(modo="incremental", estado=estado, finished_at=finished, error_detalle="TEST-salud"))
        await db.commit()


async def test_estado_solo_para_admin(
    client: AsyncClient, client_usuario: AsyncClient, client_admin: AsyncClient
) -> None:
    assert (await client.get("/admin/sync/estado")).status_code == 401
    assert (await client_usuario.get("/admin/sync/estado")).status_code == 403
    assert (await client_admin.get("/admin/sync/estado")).status_code == 200


async def test_sano_con_ok_reciente_y_no_sano_pasadas_12_horas(
    client_admin: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run("ok", datetime.now(UTC))
    await _run("failed", datetime.now(UTC))
    cuerpo = (await client_admin.get("/admin/sync/estado")).json()
    assert cuerpo["sano"] is True and cuerpo["ultimo_ok_at"] is not None
    assert {"modo", "estado", "insertadas", "peticiones_bdns"} <= set(cuerpo["ejecuciones"][0])
    monkeypatch.setattr(admin_sync, "ahora", lambda: datetime.now(UTC) + timedelta(hours=13))
    assert (await client_admin.get("/admin/sync/estado")).json()["sano"] is False


async def _parcial(admin: Sesion, motivo: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            SyncRun(
                modo="incremental", estado="partial", finished_at=datetime.now(UTC), error_detalle=motivo,
                created_by=admin.id, updated_by=admin.id,
            )
        )  # fmt: skip
        await db.commit()


async def test_parcial_solo_por_presupuesto_cuenta_como_sano(admin: Sesion, client_admin: AsyncClient) -> None:
    await _parcial(admin, "Caja de tiempo agotada.")
    assert (await client_admin.get("/admin/sync/estado")).json()["sano"] is False
    await _parcial(admin, "Retiradas masivas abortadas (3 > 2).")
    assert (await client_admin.get("/admin/sync/estado")).json()["sano"] is False
    await _parcial(admin, MOTIVO_PRESUPUESTO)
    cuerpo = (await client_admin.get("/admin/sync/estado")).json()
    assert cuerpo["sano"] is True and cuerpo["ultimo_ok_at"] is not None
