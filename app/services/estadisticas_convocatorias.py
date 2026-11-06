"""Statistics of the grants panel (`GET /convocatorias/estadisticas`, plan N10). Global figures, cached 60 s.

Rules (all over rows with `removed_at IS NULL`):

- "Open" is `estado_expr() == 'Abierta'` (same as the search and the map).
- **KPIs**: open grants, funds (sum of `presupuesto_total` of the open ones; budgets are the grant's total),
  "tecnologicas" (open and `gestiona_subvfy`) and grants closing in the next 7 days. The variation of the first three
  is what was registered (`fecha_registro`) in the last `dias` days and is still open; the variation of "cierran" is
  the 7 days ahead minus the 7 days that just passed (a signed difference), whatever the period.
- **semanas**: grants registered per week (Monday start), the last 12 weeks including the current one, oldest first;
  `tecnologicas` = those with `gestiona_subvfy`.
- **fondos**: open budget per theme, in millions, largest first; `gestiona` = theme managed by Subvfy.
- **cierres**: open grants ending each day from today for 30 days.
- **embudo**: empty; it will come from the CRM (plan N14).
"""

import time
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Convocatoria
from app.models.convocatoria import TEMAS_GESTIONADOS
from app.schemas.estadisticas import EstadisticasPanel
from app.services.clasificacion import CATALOGO_TEMAS
from app.services.convocatoria_estado import ABIERTA, estado_expr, hoy_madrid

CACHE_TTL_SEGUNDOS = 60
SEMANAS = 12
DIAS_CIERRES = 30
DIAS_PROXIMOS = 7
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
NOMBRES_TEMA = {t.codigo: t.nombre for t in CATALOGO_TEMAS}

_cache: dict[int, tuple[float, EstadisticasPanel]] = {}


def limpiar_cache() -> None:
    _cache.clear()


def _entero(n: float) -> str:
    return f"{round(n):,}".replace(",", ".")


def _con_signo(n: float, sufijo: str = "") -> str:
    return f"{'+' if n >= 0 else '-'}{_entero(abs(n))}{sufijo}"


def _dia_mes(d: date) -> str:
    return f"{d.day} {MESES[d.month - 1]}"


async def estadisticas(db: AsyncSession, dias: int) -> EstadisticasPanel:
    guardado = _cache.get(dias)
    if guardado is not None and time.monotonic() - guardado[0] <= CACHE_TTL_SEGUNDOS:
        return guardado[1]
    resultado = await _calcular(db, dias)
    _cache[dias] = (time.monotonic(), resultado)
    return resultado


async def _calcular(db: AsyncSession, dias: int) -> EstadisticasPanel:
    c = Convocatoria
    hoy: date = (await db.scalar(select(hoy_madrid()))) or date.today()
    abierta = estado_expr(hoy) == ABIERTA
    vivas = c.removed_at.is_(None)
    nueva = c.fecha_registro >= hoy - timedelta(days=dias)

    k = (
        await db.execute(
            select(
                func.count().filter(abierta),
                func.coalesce(func.sum(c.presupuesto_total).filter(abierta), 0),
                func.count().filter(abierta & c.gestiona_subvfy),
                func.count().filter(abierta & c.fecha_fin_solicitud.between(hoy, hoy + timedelta(days=7))),
                func.count().filter(abierta & nueva),
                func.coalesce(func.sum(c.presupuesto_total).filter(abierta & nueva), 0),
                func.count().filter(abierta & c.gestiona_subvfy & nueva),
                func.count().filter(c.fecha_fin_solicitud.between(hoy - timedelta(days=7), hoy - timedelta(days=1))),
            ).where(vivas)
        )
    ).one()
    abiertas, fondos, tecnologicas, cierran, v_abiertas, v_fondos, v_tec, cerraron = (float(x) for x in k)
    kpis: list[dict[str, Any]] = [
        {
            "clave": "abiertas", "etiqueta": "Convocatorias abiertas", "valor": _entero(abiertas),
            "variacion": _con_signo(v_abiertas), "pie": "en toda España",
            "valor_numero": abiertas, "variacion_numero": v_abiertas,
        },
        {
            "clave": "fondos", "etiqueta": "Fondos disponibles", "valor": _entero(fondos / 1e6) + " M€",
            "variacion": _con_signo(v_fondos / 1e6, " M€"), "pie": "presupuesto de las convocatorias abiertas",
            "valor_numero": fondos, "variacion_numero": v_fondos,
        },
        {
            "clave": "tecnologicas", "etiqueta": "Tecnológicas abiertas", "valor": _entero(tecnologicas),
            "variacion": _con_signo(v_tec), "pie": "digitalización e I+D · gestiona Subvfy",
            "valor_numero": tecnologicas, "variacion_numero": v_tec,
        },
        {
            "clave": "cierran", "etiqueta": "Cierran en 7 días", "valor": _entero(cierran),
            "variacion": _con_signo(cierran - cerraron), "pie": "frente a los 7 días anteriores",
            "valor_numero": cierran, "variacion_numero": cierran - cerraron,
        },
    ]  # fmt: skip

    # Weeks: Monday of the current week minus 11 weeks.
    lunes_actual = hoy - timedelta(days=hoy.weekday())
    primera = lunes_actual - timedelta(weeks=SEMANAS - 1)
    semana = cast(func.date_trunc("week", c.fecha_registro), Date)
    filas = (
        await db.execute(
            select(semana, func.count(), func.count().filter(c.gestiona_subvfy))
            .where(vivas, c.fecha_registro >= primera, c.fecha_registro < lunes_actual + timedelta(weeks=1))
            .group_by(semana)
        )
    ).all()
    por_semana = {d: (t, g) for d, t, g in filas}
    semanas = []
    for i in range(SEMANAS):
        lunes = primera + timedelta(weeks=i)
        total, gestionadas = por_semana.get(lunes, (0, 0))
        semanas.append({"etiqueta": _dia_mes(lunes), "todas": total, "tecnologicas": gestionadas})

    por_tema = (
        await db.execute(
            select(c.tema, func.sum(c.presupuesto_total))
            .where(vivas, abierta, c.presupuesto_total.is_not(None))
            .group_by(c.tema)
        )
    ).all()
    fondos_tema = sorted(
        (
            {"tema": NOMBRES_TEMA.get(t, t), "millones": round(float(s) / 1e6), "gestiona": t in TEMAS_GESTIONADOS}
            for t, s in por_tema
            if s
        ),
        key=lambda x: (-x["millones"], x["tema"]),
    )

    fin = c.fecha_fin_solicitud
    filas_dia = (
        await db.execute(
            select(fin, func.count())
            .where(vivas, abierta, fin.between(hoy, hoy + timedelta(days=DIAS_CIERRES - 1)))
            .group_by(fin)
        )
    ).all()
    por_dia = {d: n for d, n in filas_dia}
    cierres = [
        {
            "etiqueta": "Hoy" if i == 0 else f"Día +{i}",
            "cierres": por_dia.get(hoy + timedelta(days=i), 0),
            "proximo": i < DIAS_PROXIMOS,
        }
        for i in range(DIAS_CIERRES)
    ]
    return EstadisticasPanel.model_validate(
        {"periodo": dias, "kpis": kpis, "semanas": semanas, "fondos": fondos_tema, "cierres": cierres, "embudo": []}
    )
