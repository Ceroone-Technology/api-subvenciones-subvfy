"""Búsqueda sobre la tabla local de convocatorias (`bdns-local-sync`, PR4).

Constructor de filtros compartido: lo usan el buscador, el resumen y, más adelante,
la exportación (PR9) y el motor de alertas (PR8). Solo lee datos locales: nunca
llama a la BDNS. El estado se calcula en SQL (`estado_expr`), no se guarda.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Convocatoria, SyncRun
from app.models.convocatoria import CONFIG_BUSQUEDA
from app.services.convocatoria_estado import ABIERTA, estado_expr

c = Convocatoria
ORDENES = ("recientes", "antiguas", "codigo", "fondos", "relevancia")
OrdenConvocatorias = Literal[ORDENES]  # type: ignore[valid-type]

# Columnas del listado: todo menos `detalle`, que pesa y solo se lee en la ficha.
COLUMNAS_LISTADO = (
    c.id, c.codigo_bdns, c.titulo, c.nivel_administracion, c.administracion, c.organo_convocante,
    c.fecha_registro, c.fecha_inicio_solicitud, c.fecha_fin_solicitud, c.plazo_permanente,
    c.presupuesto_total, c.financiada_mrr, c.tipo_convocatoria, c.finalidad, c.url_portal_oficial,
)  # fmt: skip


@dataclass(frozen=True)
class FiltrosBusqueda:
    q: str | None = None
    nivel: str | None = None
    organo: Sequence[int] = ()
    region: Sequence[int] = ()
    beneficiario: Sequence[str] = ()
    estado: Sequence[str] = ()
    registro_desde: date | None = None
    registro_hasta: date | None = None
    plazo_desde: date | None = None
    plazo_hasta: date | None = None
    presupuesto_min: Decimal | None = None
    presupuesto_max: Decimal | None = None
    solo_mrr: bool = False


def _consulta_texto(q: str) -> Any:
    # `websearch_to_tsquery` no lanza con comillas ni operadores sueltos, a diferencia de `to_tsquery`.
    return func.websearch_to_tsquery(literal_column(f"'{CONFIG_BUSQUEDA}'::regconfig"), q)


def condiciones(f: FiltrosBusqueda) -> list[ColumnElement[bool]]:
    """WHERE del filtro (las retiradas nunca salen). Los criterios se acumulan con AND."""
    donde: list[ColumnElement[bool]] = [c.removed_at.is_(None)]
    if f.q and f.q.strip():
        donde.append(c.busqueda.op("@@")(_consulta_texto(f.q)))
    if f.nivel:
        donde.append(c.nivel_administracion == f.nivel)
    if f.organo:
        donde.append(c.organo_ids.overlap(list(f.organo)))
    if f.region:
        donde.append(c.region_ids.overlap(list(f.region)))
    if f.beneficiario:
        donde.append(c.beneficiarios.overlap(list(f.beneficiario)))
    if f.estado:
        donde.append(estado_expr().in_(list(f.estado)))
    rangos = (
        (c.fecha_registro, f.registro_desde, f.registro_hasta),
        (c.fecha_fin_solicitud, f.plazo_desde, f.plazo_hasta),
        (c.presupuesto_total, f.presupuesto_min, f.presupuesto_max),
    )
    for columna, minimo, maximo in rangos:
        if minimo is not None:
            donde.append(columna >= minimo)
        if maximo is not None:
            donde.append(columna <= maximo)
    if f.solo_mrr:
        donde.append(c.financiada_mrr.is_(True))
    return donde


def ordenar(orden: str, f: FiltrosBusqueda) -> list[Any]:
    """ORDER BY siempre con `id` de desempate, para que las páginas sean estables."""
    if orden == "antiguas":
        return [c.fecha_registro.asc().nulls_last(), c.id.asc()]
    if orden == "codigo":
        return [c.codigo_bdns.asc(), c.id.asc()]
    if orden == "fondos":
        return [c.presupuesto_total.desc().nulls_last(), c.id.desc()]
    if orden == "relevancia" and f.q and f.q.strip():
        return [func.ts_rank(c.busqueda, _consulta_texto(f.q)).desc(), c.id.desc()]
    # Coincide con `ix_convocatoria_registro (fecha_registro DESC, id DESC)`.
    return [c.fecha_registro.desc(), c.id.desc()]


async def buscar(
    db: AsyncSession, f: FiltrosBusqueda, orden: str, *, offset: int, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """(total del filtro completo, página). Dos consultas, vengan las filas que vengan."""
    donde = condiciones(f)
    total = await db.scalar(select(func.count()).select_from(c).where(*donde)) or 0
    filas = await db.execute(
        select(*COLUMNAS_LISTADO, estado_expr().label("estado"))
        .where(*donde)
        .order_by(*ordenar(orden, f))
        .offset(offset)
        .limit(limit)
    )
    return total, [dict(fila._mapping) for fila in filas]


async def obtener(db: AsyncSession, codigo_bdns: str) -> tuple[Convocatoria, str] | None:
    """Ficha por código (también las retiradas) con su estado calculado."""
    fila = (
        await db.execute(select(c, estado_expr().label("estado")).where(c.codigo_bdns == codigo_bdns))
    ).one_or_none()
    return None if fila is None else (fila[0], fila[1])


async def resumen(db: AsyncSession) -> dict[str, Any]:
    abierta = estado_expr() == ABIERTA
    fila = (
        await db.execute(
            select(
                func.count().filter(abierta),
                func.coalesce(func.sum(c.presupuesto_total).filter(abierta), 0),
                func.count(),
            ).where(c.removed_at.is_(None))
        )
    ).one()
    ultima = await db.scalar(select(func.max(SyncRun.finished_at)).where(and_(SyncRun.estado.in_(("ok", "partial")))))
    return {
        "abiertas": fila[0],
        "presupuesto_abiertas": float(fila[1]),
        "total": fila[2],
        "ultima_sincronizacion": ultima,
    }
