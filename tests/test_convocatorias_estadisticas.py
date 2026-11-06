"""`GET /convocatorias/estadisticas` (plan N10): KPIs, publications per week, funds per theme, closings per day.

The database is shared with real grants, so every figure is asserted as a delta against a baseline taken before
seeding (the in-memory cache is cleared between reads).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.services import estadisticas_convocatorias as servicio
from app.services.convocatoria_estado import hoy_madrid
from tests.conftest import crear_convocatoria_en_bd


async def _hoy():  # noqa: ANN202
    async with AsyncSessionLocal() as db:
        return await db.scalar(select(hoy_madrid()))


async def _leer(cliente: AsyncClient, **params: Any) -> dict[str, Any]:
    servicio.limpiar_cache()
    r = await cliente.get("/convocatorias/estadisticas", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _kpi(datos: dict[str, Any], clave: str) -> dict[str, Any]:
    return next(k for k in datos["kpis"] if k["clave"] == clave)


async def _sembrar(**extra: Any) -> None:
    hoy = await _hoy()
    base: dict[str, Any] = {
        "fecha_inicio_solicitud": hoy - timedelta(days=40),
        "fecha_fin_solicitud": hoy + timedelta(days=20),
        "fecha_registro": hoy,
        "presupuesto_total": 1_000_000,
    }
    await crear_convocatoria_en_bd("Ayudas estadisticas", **(base | extra))


async def test_requiere_login(client: AsyncClient) -> None:
    assert (await client.get("/convocatorias/estadisticas")).status_code == 401


async def test_solo_admin_y_gestor(
    client_usuario: AsyncClient, client_gestor: AsyncClient, client_admin: AsyncClient
) -> None:
    assert (await client_usuario.get("/convocatorias/estadisticas")).status_code == 403
    assert (await client_gestor.get("/convocatorias/estadisticas")).status_code == 200
    assert (await client_admin.get("/convocatorias/estadisticas")).status_code == 200


async def test_forma_y_ruta_no_se_confunde_con_el_detalle(client_gestor: AsyncClient) -> None:
    datos = await _leer(client_gestor)
    assert set(datos) == {"periodo", "kpis", "semanas", "fondos", "cierres", "embudo"}
    assert datos["periodo"] == 30 and datos["embudo"] == []
    assert [k["clave"] for k in datos["kpis"]] == ["abiertas", "fondos", "tecnologicas", "cierran"]
    assert set(datos["kpis"][0]) == {
        "clave", "etiqueta", "valor", "variacion", "pie", "valor_numero", "variacion_numero",
    }  # fmt: skip
    assert len(datos["semanas"]) == 12 and set(datos["semanas"][0]) == {"etiqueta", "todas", "tecnologicas"}
    assert len(datos["cierres"]) == 30
    assert datos["cierres"][0] == {"etiqueta": "Hoy", "cierres": datos["cierres"][0]["cierres"], "proximo": True}
    assert [c["proximo"] for c in datos["cierres"]].count(True) == 7
    assert datos["cierres"][1]["etiqueta"] == "Día +1"
    for fondo in datos["fondos"]:
        assert set(fondo) == {"tema", "millones", "gestiona"}


async def test_periodo_solo_admite_7_30_o_90(client_gestor: AsyncClient) -> None:
    for valido in (7, 30, 90):
        assert (await client_gestor.get("/convocatorias/estadisticas", params={"dias": valido})).status_code == 200
    for malo in (0, 14, 365, "x"):
        assert (await client_gestor.get("/convocatorias/estadisticas", params={"dias": malo})).status_code == 422


async def test_cifras_por_delta(client_gestor: AsyncClient) -> None:
    hoy = await _hoy()
    antes7 = await _leer(client_gestor, dias=7)
    antes30 = await _leer(client_gestor, dias=30)

    # Managed (dig), open, registered today, closes in 2 days, 500 M€.
    await _sembrar(
        tema="dig", gestiona_subvfy=True, presupuesto_total=500_000_000, fecha_fin_solicitud=hoy + timedelta(days=2)
    )
    # Other theme, open, registered 3 weeks ago, closes in 20 days.
    await _sembrar(tema="emp", fecha_registro=hoy - timedelta(days=21))
    # Already closed: not open, but it still counts as published in its week.
    await _sembrar(fecha_inicio_solicitud=hoy - timedelta(days=60), fecha_fin_solicitud=hoy - timedelta(days=3))
    # Removed from the BDNS: ignored everywhere.
    await _sembrar(tema="dig", gestiona_subvfy=True, removed_at=datetime.now(UTC))

    despues7 = await _leer(client_gestor, dias=7)
    despues30 = await _leer(client_gestor, dias=30)

    def delta(a: dict[str, Any], b: dict[str, Any], clave: str, campo: str = "valor_numero") -> float:
        return _kpi(b, clave)[campo] - _kpi(a, clave)[campo]

    assert delta(antes30, despues30, "abiertas") == 2
    assert delta(antes30, despues30, "tecnologicas") == 1
    assert delta(antes30, despues30, "fondos") == 501_000_000
    assert delta(antes30, despues30, "cierran") == 1
    # Variation: what was registered inside the period and is still open.
    assert delta(antes7, despues7, "abiertas", "variacion_numero") == 1
    assert delta(antes30, despues30, "abiertas", "variacion_numero") == 2
    assert delta(antes30, despues30, "tecnologicas", "variacion_numero") == 1
    assert _kpi(despues30, "abiertas")["variacion"].startswith(("+", "-"))
    assert _kpi(despues30, "fondos")["valor"].endswith(" M€")

    # Weeks: the last one holds today's registrations (managed + closed),
    # the 4th-from-last 3 weeks ago.
    s_antes, s_despues = antes30["semanas"], despues30["semanas"]
    assert s_despues[-1]["todas"] - s_antes[-1]["todas"] == 2
    assert s_despues[-1]["tecnologicas"] - s_antes[-1]["tecnologicas"] == 1
    assert s_despues[-4]["todas"] - s_antes[-4]["todas"] == 1
    assert [s["etiqueta"] for s in s_despues] == [s["etiqueta"] for s in s_antes]

    # Closings: only the open ones, on their day.
    assert despues30["cierres"][2]["cierres"] - antes30["cierres"][2]["cierres"] == 1
    assert despues30["cierres"][20]["cierres"] - antes30["cierres"][20]["cierres"] == 1

    # Funds per theme, in millions; Digitalización is managed by Subvfy.
    def millones(datos: dict[str, Any], tema: str) -> int:
        return next((f["millones"] for f in datos["fondos"] if f["tema"] == tema), 0)

    assert abs(millones(despues30, "Digitalización") - millones(antes30, "Digitalización") - 500) <= 1
    assert next(f for f in despues30["fondos"] if f["tema"] == "Digitalización")["gestiona"] is True
    assert all(f["gestiona"] is False for f in despues30["fondos"] if f["tema"] == "Empleo")
    todos = [f["millones"] for f in despues30["fondos"]]
    assert todos == sorted(todos, reverse=True)


async def test_cache_de_60_segundos(client_gestor: AsyncClient) -> None:
    antes = await _leer(client_gestor)
    await _sembrar()
    cacheada = (await client_gestor.get("/convocatorias/estadisticas")).json()
    assert cacheada == antes  # still the cached figures
    servicio.limpiar_cache()
    fresca = (await client_gestor.get("/convocatorias/estadisticas")).json()
    assert _kpi(fresca, "abiertas")["valor_numero"] == _kpi(antes, "abiertas")["valor_numero"] + 1
