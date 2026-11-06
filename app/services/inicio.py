"""Counters of the Inicio page (`GET /inicio/resumen`, plan N10). Read-only, computed per request.

- **novedades**: open grants first seen by the sync after the user's previous access
  (`usuario.ultimo_acceso_previo_at`, clamped to `VENTANA_MAXIMA`; `VENTANA_SIN_ACCESO` when the user has no
  previous access), not removed and not discarded by the user (`descartes.no_descartada`).
- **cierran_7_dias**: open grants whose application period ends today or in the next 7 days (Madrid), not discarded.
- **alertas_con_resultados**: active alerts of the user whose latest run found something.
- **en_seguimiento**: cards of the company's board that are not resolved yet (the three active columns).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permisos import UsuarioAutenticado
from app.models import Alerta, AlertaEjecucion, Convocatoria, Seguimiento
from app.schemas.inicio import InicioResumen
from app.services.convocatoria_estado import ABIERTA, estado_expr, hoy_madrid
from app.services.descartes import no_descartada

VENTANA_MAXIMA = timedelta(days=30)
VENTANA_SIN_ACCESO = timedelta(days=7)
DIAS_CIERRE = 7


def desde_de(previo: datetime | None, ahora: datetime) -> datetime:
    if previo is None:
        return ahora - VENTANA_SIN_ACCESO
    return max(previo, ahora - VENTANA_MAXIMA)


async def resumen(db: AsyncSession, actual: UsuarioAutenticado, *, ahora: datetime | None = None) -> InicioResumen:
    ahora = ahora or datetime.now(UTC)
    c = Convocatoria
    desde = desde_de(actual.usuario.ultimo_acceso_previo_at, ahora)
    visible = (c.removed_at.is_(None), estado_expr() == ABIERTA, no_descartada(actual.id))

    novedades = await db.scalar(
        select(func.count()).where(*visible, func.coalesce(c.first_seen_at, c.sincronizado_at) > desde)
    )
    hoy = hoy_madrid()
    cierran = await db.scalar(
        select(func.count()).where(*visible, c.fecha_fin_solicitud >= hoy, c.fecha_fin_solicitud <= hoy + DIAS_CIERRE)
    )

    ultima = (
        select(AlertaEjecucion.alerta_id, AlertaEjecucion.convocatorias_encontradas)
        .join(Alerta, Alerta.id == AlertaEjecucion.alerta_id)
        .where(Alerta.usuario_id == actual.id, Alerta.activa.is_(True))
        .distinct(AlertaEjecucion.alerta_id)
        .order_by(AlertaEjecucion.alerta_id, AlertaEjecucion.fecha_ejecucion_at.desc(), AlertaEjecucion.id.desc())
        .subquery()
    )
    con_resultados = await db.scalar(select(func.count()).where(ultima.c.convocatorias_encontradas > 0))

    en_seguimiento = await db.scalar(
        select(func.count()).where(Seguimiento.empresa_id == actual.empresa_id, Seguimiento.estado != "resuelta")
    )
    return InicioResumen(
        novedades=novedades or 0,
        cierran_7_dias=cierran or 0,
        alertas_con_resultados=con_resultados or 0,
        en_seguimiento=en_seguimiento or 0,
        novedades_desde=desde,
    )
