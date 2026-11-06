"""Backfill of `convocatoria.cnae_secciones` for rows synced before the column existed (PR B2).

The stored `sectores` are descriptions only, so the sections cannot be derived offline: the detail is
fetched again from the BDNS (same client, throttle and circuit breaker as the sync) and only
`cnae_secciones` is written. `contenido_hash` is left alone: the next regular detail refresh sees the
new hash, rewrites the row once and converges. Safe to repeat; rows whose codes do not map stay empty.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Convocatoria
from app.services.bdns_cliente import BdnsNoDisponible
from app.services.bdns_sync_cliente import CircuitoAbierto, ClienteBdnsSync
from app.services.cnae import secciones_de

logger = logging.getLogger(__name__)

TODOS = ""  # `prefijo` of the real run: every code (a test passes its own, to keep to its rows)


class ClienteDetalle(Protocol):
    async def detalle(self, codigo_bdns: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class ResultadoRelleno:
    actualizadas: int
    sin_seccion: int  # retired by the BDNS, or none of its sector codes maps to a section
    errores: int
    interrumpido: bool  # the circuit breaker opened: re-run later


async def rellenar_cnae(db: AsyncSession, cliente: ClienteDetalle, *, limite: int, prefijo: str) -> ResultadoRelleno:
    c = Convocatoria
    pendientes = (
        await db.scalars(
            select(c.codigo_bdns)
            .where(
                c.removed_at.is_(None),
                c.codigo_bdns.like(f"{prefijo}%"),
                func.cardinality(c.sectores) > 0,
                func.cardinality(c.cnae_secciones) == 0,
            )
            .order_by(c.id)
            .limit(limite)
        )
    ).all()
    actualizadas = sin_seccion = errores = 0
    interrumpido = False
    for codigo in pendientes:
        try:
            detalle = await cliente.detalle(codigo)
        except CircuitoAbierto:
            interrumpido = True
            break
        except BdnsNoDisponible:
            errores += 1
            continue
        secciones = secciones_de((detalle or {}).get("sectores"))
        if not secciones:
            sin_seccion += 1
            continue
        await db.execute(update(c).where(c.codigo_bdns == codigo).values(cnae_secciones=secciones))
        await db.commit()
        actualizadas += 1
    logger.info("CNAE: %d actualizadas, %d sin sección, %d errores.", actualizadas, sin_seccion, errores)
    return ResultadoRelleno(actualizadas, sin_seccion, errores, interrumpido)


async def ejecutar_relleno(*, limite: int) -> ResultadoRelleno:
    """Entry point of the CLI: builds the BDNS client and the session from the configuration."""
    async with ClienteBdnsSync(
        rps=settings.bdns_sync_rps,
        intentos=settings.bdns_sync_reintentos,
        umbral_breaker=settings.bdns_sync_breaker,
        vpd=settings.bdns_vpd,
    ) as cliente, AsyncSessionLocal() as db:
        return await rellenar_cnae(db, cliente, limite=limite, prefijo=TODOS)
