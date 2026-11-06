"""Radar of opportunities and `orden=afinidad` (N3): database glue around the pure score in `afinidad`.

Scores are **never stored**. Each call runs an SQL prefilter (open, not removed, the caller's own filters), loads
only the columns the score reads, scores the rows in Python and keeps the best. The prefilter is capped
(`MAX_CANDIDATAS`, ordered by the soonest deadline for the radar and by the newest registration for the search) so
a table that keeps growing cannot make one request unbounded.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CatalogoBdns, Convocatoria, Empresa
from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA
from app.services import busqueda_convocatorias as busqueda
from app.services.afinidad import PerfilAfinidad, ResultadoAfinidad, SenalesConvocatoria, calcular
from app.services.busqueda_convocatorias import COLUMNAS_LISTADO, FiltrosBusqueda
from app.services.convocatoria_estado import ABIERTA, ABIERTA_SIN_PLAZO, estado_expr, hoy_madrid
from app.services.empresas import intereses_de
from app.services.zonas import comunidad_de_empresa, comunidad_de_nuts, comunidades_de_regiones

c = Convocatoria
MAX_CANDIDATAS = 10_000
ESTADOS_ABIERTOS = (ABIERTA, ABIERTA_SIN_PLAZO)
_DIAS = (c.fecha_fin_solicitud - hoy_madrid()).label("dias_restantes")
_COLUMNAS_SENALES = (
    c.id, c.codigo_bdns, c.titulo, c.organo_convocante, c.administracion, c.nivel_administracion, c.tema,
    c.gestiona_subvfy, c.cnae_secciones, (func.cardinality(c.sectores) > 0).label("tiene_sectores"), c.region_ids,
    c.beneficiarios, c.presupuesto_total,
)  # fmt: skip


@dataclass(frozen=True)
class Oportunidad:
    fila: dict[str, Any]
    resultado: ResultadoAfinidad
    dias_restantes: int | None


async def mapa_comunidades(db: AsyncSession) -> dict[int, str]:
    """BDNS region id -> community, from the NUTS-2 nodes of the local catalogue (a handful of rows)."""
    filas = await db.execute(
        select(CatalogoBdns.bdns_id, CatalogoBdns.codigo).where(
            CatalogoBdns.tipo == "region", func.length(CatalogoBdns.codigo) == 4
        )
    )
    return {bdns_id: comunidad for bdns_id, codigo in filas if (comunidad := comunidad_de_nuts(codigo)) is not None}


async def perfil_de(db: AsyncSession, empresa_id: int) -> PerfilAfinidad:
    empresa = await db.get(Empresa, empresa_id)
    if empresa is None:  # a user always has a company; defensive for a half-deleted tenant
        return PerfilAfinidad()
    intereses = (await intereses_de(db, [empresa_id])).get(empresa_id, [])
    return PerfilAfinidad(
        actividad_codigo=empresa.actividad_codigo,
        tamano=empresa.tamano,
        comunidad=comunidad_de_empresa(empresa.ccaa, empresa.provincia),
        intereses=frozenset(intereses),
    )


def seccion_para_filtrar(perfil: PerfilAfinidad) -> str | None:
    """The CNAE section to prefilter by; none when the activity is pending or `OTRA` (nothing to match on)."""
    codigo = perfil.actividad_codigo
    return None if codigo is None or codigo == CODIGO_ACTIVIDAD_OTRA else codigo


def _senales(fila: dict[str, Any], por_region: dict[int, str]) -> SenalesConvocatoria:
    return SenalesConvocatoria(
        tema=fila["tema"],
        cnae_secciones=fila["cnae_secciones"],
        tiene_sectores=bool(fila["tiene_sectores"]),
        comunidades=comunidades_de_regiones(fila["region_ids"], por_region),
        estatal=fila["nivel_administracion"] == "estado",
        beneficiarios=fila["beneficiarios"],
        dias_restantes=fila["dias_restantes"],
    )


def puntuar(filas: Sequence[dict[str, Any]], perfil: PerfilAfinidad, por_region: dict[int, str]) -> list[Oportunidad]:
    return [
        Oportunidad(fila, calcular(perfil, _senales(fila, por_region)), fila["dias_restantes"]) for fila in filas
    ]


def _orden_mejor(o: Oportunidad) -> tuple[int, int, int]:
    """Best affinity first; ties: soonest deadline (no deadline last), then the lower id (stable pages)."""
    return (-o.resultado.afinidad, o.dias_restantes if o.dias_restantes is not None else 10**9, o.fila["id"])


async def _candidatas(
    db: AsyncSession, donde: list[ColumnElement[bool]], orden: list[Any]
) -> list[dict[str, Any]]:
    filas = await db.execute(select(*_COLUMNAS_SENALES, _DIAS).where(*donde).order_by(*orden).limit(MAX_CANDIDATAS))
    return [dict(f._mapping) for f in filas]


async def radar(
    db: AsyncSession,
    empresa_id: int,
    *,
    minimo: int = 0,
    temas: Sequence[str] = (),
    gestiona: bool | None = None,
    limite: int = 50,
) -> tuple[int, list[Oportunidad]]:
    """`(how many reach `minimo`, the best `limite` of them)` for a company, open calls only."""
    perfil = await perfil_de(db, empresa_id)
    seccion = seccion_para_filtrar(perfil)
    filtros = FiltrosBusqueda(
        estado=ESTADOS_ABIERTOS, tema=tuple(temas), gestiona=gestiona, actividad=(seccion,) if seccion else ()
    )
    candidatas = await _candidatas(db, busqueda.condiciones(filtros), busqueda.ordenar("cierre", filtros))
    puntuadas = [o for o in puntuar(candidatas, perfil, await mapa_comunidades(db)) if o.resultado.afinidad >= minimo]
    puntuadas.sort(key=_orden_mejor)
    return len(puntuadas), puntuadas[:limite]


async def buscar_por_afinidad(
    db: AsyncSession, f: FiltrosBusqueda, empresa_id: int, *, offset: int, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """`orden=afinidad` of the search: same rows and `total` as `busqueda.buscar`, best affinity first.

    Each returned row carries its `afinidad`. Every filter of the search applies (a closed grant simply scores low
    on the deadline block); only the first `MAX_CANDIDATAS` rows by registration date are scored.
    """
    donde = busqueda.condiciones(f)
    total = await db.scalar(select(func.count()).select_from(c).where(*donde)) or 0
    perfil = await perfil_de(db, empresa_id)
    candidatas = await _candidatas(db, donde, busqueda.ordenar("recientes", f))
    puntuadas = puntuar(candidatas, perfil, await mapa_comunidades(db))
    puntuadas.sort(key=_orden_mejor)
    pagina = puntuadas[offset : offset + limit]
    if not pagina:
        return total, []
    por_id = {o.fila["id"]: o.resultado.afinidad for o in pagina}
    filas = await db.execute(
        select(*COLUMNAS_LISTADO, estado_expr().label("estado")).where(c.id.in_(list(por_id)))
    )
    completas = {fila.id: dict(fila._mapping) | {"afinidad": por_id[fila.id]} for fila in filas}
    return total, [completas[o.fila["id"]] for o in pagina if o.fila["id"] in completas]
