"""`estado_expr`: estado computed in SQL against today in Europe/Madrid (spec: Computed estado)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Convocatoria
from app.services.convocatoria_estado import ESTADOS_CONVOCATORIA, estado_expr, hoy_madrid
from tests.conftest import crear_convocatoria_en_bd

HOY = date(2026, 10, 20)
D = date.fromisoformat

CASOS = [
    pytest.param(None, D("2026-10-20"), False, "Abierta", id="ultimo-dia-inclusive"),
    pytest.param(None, D("2026-10-19"), False, "Cerrada", id="fin-ayer"),
    pytest.param(D("2026-10-20"), D("2026-10-20"), False, "Abierta", id="inicio-y-fin-hoy"),
    pytest.param(D("2026-10-21"), D("2026-10-30"), False, "Pendiente", id="bandera-bdns-obsoleta"),
    pytest.param(D("2026-10-01"), D("2026-10-30"), False, "Abierta", id="ventana-vigente"),
    pytest.param(D("2026-10-21"), None, False, "Pendiente", id="inicio-futuro-sin-fin"),
    pytest.param(None, None, True, "Abierta sin plazo", id="sin-fechas-permanente"),
    pytest.param(None, None, False, "Plazo no especificado", id="sin-fechas"),
    pytest.param(D("2026-10-01"), None, True, "Abierta", id="inicio-pasado-sin-fin-abierto"),
    pytest.param(D("2026-10-01"), None, False, "Plazo no especificado", id="inicio-pasado-sin-fin"),
]


@pytest.mark.parametrize(("inicio", "fin", "permanente", "esperado"), CASOS)
async def test_estado_por_fechas(inicio: date | None, fin: date | None, permanente: bool, esperado: str) -> None:
    fila = await crear_convocatoria_en_bd(
        fecha_inicio_solicitud=inicio,
        fecha_fin_solicitud=fin,
        plazo_permanente=permanente,
        abierto_bdns=False,  # la bandera de la BDNS se ignora
    )
    async with AsyncSessionLocal() as db:
        estado = await db.scalar(select(estado_expr(hoy=HOY)).where(Convocatoria.id == fila.id))
    assert estado == esperado
    assert esperado in ESTADOS_CONVOCATORIA


@pytest.mark.parametrize(
    ("instante", "esperado"),
    [
        (datetime(2026, 10, 20, 21, 30, tzinfo=UTC), date(2026, 10, 20)),  # 23:30 en Madrid
        (datetime(2026, 10, 20, 22, 30, tzinfo=UTC), date(2026, 10, 21)),  # 00:30 en Madrid, no el día UTC
        (datetime(2026, 12, 20, 22, 30, tzinfo=UTC), date(2026, 12, 20)),  # invierno: UTC+1
    ],
)
async def test_hoy_se_mide_en_madrid_y_no_en_utc(instante: datetime, esperado: date) -> None:
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(hoy_madrid(ahora=instante))) == esperado


async def test_sin_hoy_explicito_usa_la_fecha_de_madrid_de_la_base() -> None:
    fila = await crear_convocatoria_en_bd(fecha_fin_solicitud=date(2999, 1, 1))
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(estado_expr()).where(Convocatoria.id == fila.id)) == "Abierta"
