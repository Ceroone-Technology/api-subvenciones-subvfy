"""Ventana de la selección de novedades del motor de alertas: `_desde_de`."""

from datetime import UTC, datetime, timedelta

from app.config import settings
from app.models import Alerta
from app.services.motor_alertas import _desde_de


def test_desde_retrocede_un_margen_para_no_perder_filas_que_se_confirmaron_tarde() -> None:
    # `disponible_at` lo pone el sync al escribir; si el commit llega tras el ciclo, la fila queda con una
    # marca anterior a `ultima_ejecucion_at`. El margen la recoge; la deduplicación evita el aviso doble.
    ultima = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    alerta = Alerta(ultima_ejecucion_at=ultima)
    assert _desde_de(alerta) == ultima - timedelta(minutes=settings.alertas_solape_minutos)
    assert settings.alertas_solape_minutos >= settings.sync_max_minutos  # cubre un sync entero


def test_la_primera_vez_sigue_usando_la_ventana_de_primera_ejecucion() -> None:
    desde = _desde_de(Alerta(ultima_ejecucion_at=None))
    esperado = datetime.now(UTC) - timedelta(days=settings.bdns_dias_primera_ejecucion)
    assert abs(desde - esperado) < timedelta(seconds=5)
