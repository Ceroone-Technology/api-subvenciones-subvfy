"""Global map of the grants (`GET /convocatorias/mapa`): totals per community over the filtered set.

Uses the exact filter builder of the search (`busqueda_convocatorias.condiciones`). One aggregate query, no rows
transferred. Rules:

- **Community**: a grant belongs to a community when its `region_ids` contain a community node of the catalogue
  (provinces and municipalities carry their ancestors, so they roll up). A grant covering several communities counts
  once in each, so **the sum of communities can exceed `total`**.
- **Nationwide** (`estatal`): `nivel_administracion = 'estado'`, the same signal the radar uses. It is not exclusive:
  such a grant that also lists communities counts there too.
- **Without zone** (`sin_zona`): not nationwide and in no community.
- `abiertas` follows `estado_expr() == 'abierta'`; budgets are the grant's total, never split between communities.
- `region_id` is the lowest catalogue id of the community node: the value the `region` filter takes.
"""

import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Convocatoria
from app.services.busqueda_convocatorias import FiltrosBusqueda, condiciones
from app.services.convocatoria_estado import ABIERTA, estado_expr
from app.services.radar import mapa_comunidades
from app.services.zonas import NOMBRES_COMUNIDAD

CACHE_TTL_SEGUNDOS = 60
CACHE_MAX_ENTRADAS = 128

_cache: OrderedDict[Hashable, tuple[float, dict[str, Any]]] = OrderedDict()


def limpiar_cache() -> None:
    _cache.clear()


def tamano_cache() -> int:
    return len(_cache)


def _guardar(clave: Hashable, valor: dict[str, Any]) -> None:
    _cache[clave] = (time.monotonic(), valor)
    _cache.move_to_end(clave)
    while len(_cache) > CACHE_MAX_ENTRADAS:
        _cache.popitem(last=False)


def _leer(clave: Hashable) -> dict[str, Any] | None:
    hallado = _cache.get(clave)
    if hallado is None:
        return None
    guardado, valor = hallado
    if time.monotonic() - guardado > CACHE_TTL_SEGUNDOS:
        del _cache[clave]
        return None
    return valor


def clave_de(f: FiltrosBusqueda) -> Hashable:
    """Hashable, order-insensitive key of the filters (list filters are sets)."""
    partes = []
    for nombre, valor in sorted(asdict(f).items()):
        partes.append((nombre, tuple(sorted(set(valor))) if isinstance(valor, list | tuple) else valor))
    return tuple(partes)


def _num(valor: Decimal | int | float | None) -> int | float:
    if valor is None:
        return 0
    return int(valor) if valor == int(valor) else float(valor)


async def mapa(db: AsyncSession, f: FiltrosBusqueda) -> dict[str, Any]:
    clave = clave_de(f)
    if (hecho := _leer(clave)) is not None:
        return hecho
    resultado = await _calcular(db, f)
    _guardar(clave, resultado)
    return resultado


async def _calcular(db: AsyncSession, f: FiltrosBusqueda) -> dict[str, Any]:
    c = Convocatoria
    por_region = await mapa_comunidades(db)
    nodos: dict[str, list[int]] = {}
    for bdns_id, zona in sorted(por_region.items()):
        nodos.setdefault(zona, []).append(bdns_id)

    # Status and nationwide (never NULL) computed once per row in the subquery; the aggregates below only read booleans.
    base = (
        select(
            c.region_ids.label("regiones"),
            c.presupuesto_total.label("presupuesto"),
            (estado_expr() == ABIERTA).label("abierta"),
            func.coalesce(c.nivel_administracion == "estado", false()).label("es_estatal"),
        )
        .where(*condiciones(f))
        .subquery()
    )
    b = base.c

    def agregados(en: ColumnElement[bool] | None) -> list[Any]:
        if en is None:
            return [func.count(), func.count().filter(b.abierta), func.coalesce(func.sum(b.presupuesto), 0)]
        return [
            func.count().filter(en),
            func.count().filter(en & b.abierta),
            func.coalesce(func.sum(b.presupuesto).filter(en), 0),
        ]

    zonas = sorted(nodos)
    en_alguna: ColumnElement[bool] = b.regiones.overlap(sorted(por_region)) if por_region else false()
    columnas: list[Any] = [*agregados(None), *agregados(b.es_estatal)]
    columnas.append(func.count().filter(~b.es_estatal & ~en_alguna))
    for z in zonas:
        columnas.extend(agregados(b.regiones.overlap(nodos[z])))
    fila = (await db.execute(select(*columnas))).one()

    comunidades = []
    for i, z in enumerate(zonas):
        total, abiertas, presupuesto = fila[7 + 3 * i : 10 + 3 * i]
        if total:
            comunidades.append(
                {
                    "region_id": nodos[z][0],
                    "nombre": NOMBRES_COMUNIDAD[z],
                    "total": total,
                    "abiertas": abiertas,
                    "presupuesto": _num(presupuesto),
                }
            )
    comunidades.sort(key=lambda x: (-x["total"], x["nombre"]))
    return {
        "total": fila[0],
        "presupuesto_total": _num(fila[2]),
        "por_comunidad": comunidades,
        "estatal": {"total": fila[3], "abiertas": fila[4], "presupuesto": _num(fila[5])},
        "sin_zona": {"total": fila[6]},
    }
