"""Global map of the grants (`GET /convocatorias/mapa`): totals per community over the filtered set.

Uses the exact filter builder of the search (`busqueda_convocatorias.condiciones`). One aggregate query, no rows
transferred. Rules:

- **Community**: a grant belongs to a community when its `region_ids` contain a community node of the catalogue
  (provinces and municipalities carry their ancestors, so they roll up). A grant covering several communities counts
  once in each, so **the sum of communities can exceed `total`**.
- **Macro-regions** (`ES1`..`ES7`, NUTS-1 nodes): every region id carries its ancestors, so a macro node is always
  present next to its children. It only counts when it is the grant's most specific node under that macro (no
  descendant of it in `region_ids`); then the grant rolls up to every community of the macro (ES1 Galicia, Asturias,
  Cantabria; ES2 País Vasco, Navarra, La Rioja, Aragón; ES3 Madrid; ES4 Castilla y León, Castilla-La Mancha,
  Extremadura; ES5 Cataluña, C. Valenciana, Baleares; ES6 Andalucía, Murcia; ES7 Canarias).
- **Abroad** (`exterior`): every region id is outside Spain (`EXTRA-REGIO` nodes, `UK`, `XXX`, `XXXX`, any code that
  is not `ES`/`ES<digits>`). All grants are Spanish: this means the project takes place abroad. Such a grant is never
  in `estatal`, `sin_zona` or a community. A grant that mixes Spanish and foreign regions is a normal Spanish one.
- **Nationwide** (`estatal`), not abroad and either `nivel_administracion = 'estado'` (the radar signal) or its
  regions are only the root `ES` (region España). It is not exclusive: such a grant that also lists communities
  counts there too.
- **Without zone** (`sin_zona`): not nationwide, not abroad and in no community.
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

from app.models import CatalogoBdns, Convocatoria
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


def _es_espana(codigo: str) -> bool:
    return codigo == "ES" or (codigo.startswith("ES") and codigo[2:].isdigit())


Estructura = tuple[list[int], list[int], dict[int, list[int]], dict[int, list[int]]]


async def _estructura_regiones(db: AsyncSession) -> Estructura:
    """From the region catalogue: root `ES` ids, foreign ids, and per macro node its descendants and its communities."""
    filas = (
        await db.execute(
            select(CatalogoBdns.bdns_id, CatalogoBdns.codigo, CatalogoBdns.padre_bdns_id).where(
                CatalogoBdns.tipo == "region", CatalogoBdns.codigo.is_not(None)
            )
        )
    ).all()
    raices = sorted(i for i, codigo, _ in filas if codigo == "ES")
    exterior = sorted(i for i, codigo, _ in filas if codigo is not None and not _es_espana(codigo.upper()))
    padre = {i: p for i, _, p in filas}

    def desciende_de(i: int, ancestro: int) -> bool:
        visto: set[int] = set()
        while (i := padre.get(i)) is not None and i not in visto:  # type: ignore[assignment]
            if i == ancestro:
                return True
            visto.add(i)
        return False

    descendientes: dict[int, list[int]] = {}
    comunidades: dict[int, list[int]] = {}
    for i, codigo, _ in filas:
        if codigo is None or len(codigo) != 3 or not _es_espana(codigo.upper()) or codigo.upper() == "ES":
            continue
        descendientes[i] = sorted(j for j, _, _ in filas if desciende_de(j, i))
        prefijo = codigo.upper()
        comunidades[i] = sorted(j for j, c, _ in filas if c and len(c) == 4 and c.upper().startswith(prefijo))
    return raices, exterior, descendientes, comunidades


async def _calcular(db: AsyncSession, f: FiltrosBusqueda) -> dict[str, Any]:
    c = Convocatoria
    por_region = await mapa_comunidades(db)
    nodos: dict[str, list[int]] = {}
    for bdns_id, zona in sorted(por_region.items()):
        nodos.setdefault(zona, []).append(bdns_id)
    raices, exterior_ids, descendientes, comunidades_de_macro = await _estructura_regiones(db)

    # Status, nationwide and abroad (never NULL) computed once per row in the subquery; aggregates only read booleans.
    base_cols = select(
        c.region_ids.label("regiones"),
        c.presupuesto_total.label("presupuesto"),
        (estado_expr() == ABIERTA).label("abierta"),
        func.coalesce(c.nivel_administracion == "estado", false()).label("es_estatal"),
    ).where(*condiciones(f))
    base = base_cols.subquery()
    b = base.c

    def solo_en(ids: list[int]) -> ColumnElement[bool]:
        return b.regiones.contained_by(ids) if ids else false()

    def toca(ids: list[int]) -> ColumnElement[bool]:
        return b.regiones.overlap(ids) if ids else false()

    en_el_exterior: ColumnElement[bool] = func.cardinality(b.regiones) > 0
    en_el_exterior = en_el_exterior & solo_en(exterior_ids)
    solo_raiz_o_fuera = toca(raices) & solo_en([*raices, *exterior_ids])
    estatal = (b.es_estatal | solo_raiz_o_fuera) & ~en_el_exterior

    # A community is covered by one of its own nodes, or by a macro node that is the most specific one of the grant.
    def pertenece(zona: str) -> ColumnElement[bool]:
        condicion = toca(nodos[zona])
        for macro, destino in comunidades_de_macro.items():
            if any(i in nodos[zona] for i in destino):
                condicion = condicion | (toca([macro]) & ~toca(descendientes.get(macro, [])))
        return condicion

    zonas = sorted(nodos)
    pertenencias = {z: pertenece(z) for z in zonas}
    en_alguna: ColumnElement[bool] = false()
    for z in zonas:
        en_alguna = en_alguna | pertenencias[z]

    def agregados(en: ColumnElement[bool] | None) -> list[Any]:
        if en is None:
            return [func.count(), func.count().filter(b.abierta), func.coalesce(func.sum(b.presupuesto), 0)]
        return [
            func.count().filter(en),
            func.count().filter(en & b.abierta),
            func.coalesce(func.sum(b.presupuesto).filter(en), 0),
        ]

    columnas: list[Any] = [*agregados(None), *agregados(estatal)]
    columnas.append(func.count().filter(~estatal & ~en_el_exterior & ~en_alguna))
    columnas.extend(agregados(en_el_exterior))
    for z in zonas:
        columnas.extend(agregados(pertenencias[z]))
    fila = (await db.execute(select(*columnas))).one()

    comunidades = []
    for i, z in enumerate(zonas):
        total, abiertas, presupuesto = fila[10 + 3 * i : 13 + 3 * i]
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
        "exterior": {"total": fila[7], "abiertas": fila[8], "presupuesto": _num(fila[9])},
    }
