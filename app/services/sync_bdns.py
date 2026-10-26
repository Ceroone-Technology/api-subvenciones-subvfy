"""Motor de sincronización de la BDNS (`bdns-local-sync`, PR3).

Servicio independiente del scheduler (mismo seam que `motor_alertas`): lo llaman
el cron, el CLI y, en el Hito 7, una Lambda.

- **Un solo run a la vez** (D6): `pg_try_advisory_lock` sobre una conexión
  **dedicada** que vive todo el run. Con `NullPool` cada commit de la sesión de
  trabajo cierra su conexión, y un lock de sesión se perdería con ella.
- **Fase A**: recorre la ventana por `fechaRecepcion` ascendente (D8), en trozos
  (mensuales en backfill), guarda el resumen y deja el checkpoint en
  `sync_run.cursor` tras cada página. `cursor` con `pagina` = fase A sin terminar.
- **Fase B**: detalle de las filas nuevas (`detalle_sync_at IS NULL`), más nuevas primero.
- **Fase C** (solo incremental): refresco rotatorio de las convocatorias abiertas, pendientes
  o recientes sin plazo, las de `detalle_sync_at` más viejo primero, con presupuesto.
- Upsert idempotente por `contenido_hash`; la fila nunca se borra (`removed_at`).

Todas las marcas de tiempo salen de `ahora()` para poder probarlo con un reloj falso.
"""

import calendar
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Boolean, and_, func, literal, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import settings
from app.database import engine
from app.models import CatalogoBdns, Convocatoria, SyncRun
from app.services.bdns_cliente import BdnsNoDisponible, ConvocatoriaBdns
from app.services.bdns_mapper import DetalleInvalido, etiquetas_region, mapear_detalle, ruta_organo
from app.services.bdns_sync_cliente import CircuitoAbierto, ClienteBdnsSync
from app.services.catalogos import ResolvedorCatalogo, cargar_resolvedor, refrescar_catalogos
from app.services.deduplicacion_alertas import NIVEL_BDNS_A_NUESTRO
from app.services.sistema import id_usuario_sistema

logger = logging.getLogger(__name__)

CLAVE_LOCK = "hashtext('sync_bdns')"
LOTE_B = 500
COMMIT_CADA = 25
DIAS_SIN_PLAZO_RECIENTE = 90
REFRESCO_CATALOGOS = timedelta(hours=24)
# Campos del detalle que, si el detalle no los trae, conservan lo ya guardado (COALESCE).
CONSERVAR = ("nivel_administracion", "administracion", "organo_convocante", "fecha_registro", "bdns_id")


@dataclass(frozen=True)
class ResultadoSync:
    run_id: int
    estado: str


def sumar_meses(dia: date, meses: int) -> date:
    indice = dia.year * 12 + dia.month - 1 + meses
    anio, mes = divmod(indice, 12)
    return date(anio, mes + 1, min(dia.day, calendar.monthrange(anio, mes + 1)[1]))


async def ejecutar_sync(
    *, modo: str = "incremental", meses: int | None = None, max_minutos: int | None = None
) -> ResultadoSync | None:
    """Punto de entrada de cron y CLI: crea el cliente desde la configuración."""
    async with ClienteBdnsSync(
        rps=settings.bdns_sync_rps,
        intentos=settings.bdns_sync_reintentos,
        umbral_breaker=settings.bdns_sync_breaker,
        vpd=settings.bdns_vpd,
    ) as cliente:
        return await ejecutar(cliente, modo=modo, meses=meses, max_minutos=max_minutos)


async def ejecutar(
    cliente: ClienteBdnsSync,
    *,
    modo: str = "incremental",
    meses: int | None = None,
    max_minutos: int | None = None,
    presupuesto_refresco: int | None = None,
    usuario_id: int | None = None,
    ahora: Callable[[], datetime] = lambda: datetime.now(UTC),
    hoy: date | None = None,
    desde: date | None = None,
    # Solo procesa los códigos con este prefijo (los tests aíslan así sus filas de las reales).
    prefijo: str = "",
    motor: AsyncEngine = engine,
) -> ResultadoSync | None:
    """Un run completo. Devuelve `None` si otro run tiene el lock (sin tocar la BDNS)."""
    async with motor.connect() as conexion:
        await conexion.execution_options(isolation_level="AUTOCOMMIT")
        if not await conexion.scalar(text(f"SELECT pg_try_advisory_lock({CLAVE_LOCK})")):
            logger.info("Sync BDNS: locked, ya hay un run en curso.")
            return None
        try:
            return await _con_lock(
                cliente, async_sessionmaker(motor, expire_on_commit=False), modo, meses, max_minutos,
                presupuesto_refresco, usuario_id, ahora, hoy, desde, prefijo,
            )  # fmt: skip
        finally:
            await conexion.execute(text(f"SELECT pg_advisory_unlock({CLAVE_LOCK})"))


async def _con_lock(
    cliente: ClienteBdnsSync,
    sesiones: async_sessionmaker[AsyncSession],
    modo: str,
    meses: int | None,
    max_minutos: int | None,
    presupuesto: int | None,
    usuario_id: int | None,
    ahora: Callable[[], datetime],
    hoy: date | None,
    desde: date | None,
    prefijo: str,
) -> ResultadoSync:
    inicio = ahora()
    limite = inicio + timedelta(minutes=settings.sync_max_minutos if max_minutos is None else max_minutos)
    presupuesto = settings.sync_presupuesto_refresco if presupuesto is None else presupuesto
    async with sesiones() as db:
        uid = usuario_id or await id_usuario_sistema(db)
        # El lock garantiza que nadie más corre: un `running` anterior es de un proceso muerto.
        await db.execute(
            update(SyncRun)
            .where(SyncRun.estado == "running", SyncRun.created_by == uid)
            .values(estado="failed", finished_at=inicio, error_detalle="Interrumpida sin cerrar el run.")
        )
        hoy = hoy or await db.scalar(select(func.date(func.timezone(settings.sync_zona, inicio)))) or inicio.date()
        run = SyncRun(modo=modo, created_by=uid, updated_by=uid, started_at=inicio)
        await _planificar(db, run, uid, hoy, meses or settings.sync_backfill_meses, desde)
        db.add(run)
        await db.commit()
        ctx = _Ctx(db, run, cliente, uid, ahora, limite, prefijo)
        error: str | None = None
        try:
            await _fases(ctx, hoy, presupuesto)
            estado = "partial" if ctx.parcial else "ok"
        except CircuitoAbierto as exc:
            estado, error = "failed", str(exc)
        except Exception as exc:
            await db.rollback()
            await _cerrar_fallida(sesiones, run.id, f"{type(exc).__name__}: {exc}")
            raise
        run.estado, run.finished_at, run.error_detalle = estado, ahora(), (error or ctx.motivo)
        run.peticiones_bdns = cliente.peticiones
        await db.commit()
        return ResultadoSync(run.id, estado)


async def _cerrar_fallida(sesiones: async_sessionmaker[AsyncSession], run_id: int, mensaje: str) -> None:
    async with sesiones() as db:
        await db.execute(
            update(SyncRun).where(SyncRun.id == run_id).values(estado="failed", error_detalle=mensaje[:500])
        )
        await db.commit()


async def _planificar(db: AsyncSession, run: SyncRun, uid: int, hoy: date, meses: int, desde: date | None) -> None:
    """Ventana y cursor: retoma el de la última ejecución con la fase A sin terminar."""
    filtro = (SyncRun.modo == run.modo, SyncRun.created_by == uid, SyncRun.estado != "running")
    ultima = await db.scalar(select(SyncRun).where(*filtro).order_by(SyncRun.id.desc()).limit(1))
    if ultima is not None and ultima.cursor and "pagina" in ultima.cursor:
        run.cursor = dict(ultima.cursor)
        run.ventana_desde = date.fromisoformat(run.cursor["desde"])
        run.ventana_hasta = date.fromisoformat(run.cursor["hasta"])
        return
    if run.modo == "backfill":
        inicio = sumar_meses(hoy, -meses)
    elif desde is not None:
        inicio = desde
    else:
        # Marca de agua (D7): el mayor `ventana_hasta` de los runs con la fase A completa.
        hecha = ~func.coalesce(SyncRun.cursor.has_key("pagina"), False)
        marca = await db.scalar(select(func.max(SyncRun.ventana_hasta)).where(*filtro, hecha))
        inicio = (
            marca - timedelta(days=settings.sync_solape_dias)
            if marca
            else hoy - timedelta(days=settings.bdns_dias_primera_ejecucion)
        )
    run.ventana_desde, run.ventana_hasta = inicio, hoy
    run.cursor = {"desde": inicio.isoformat(), "hasta": hoy.isoformat(), "trozo": inicio.isoformat(), "pagina": 0}


class _Ctx:
    def __init__(
        self,
        db: AsyncSession,
        run: SyncRun,
        cliente: ClienteBdnsSync,
        uid: int,
        ahora: Callable[[], datetime],
        limite: datetime,
        prefijo: str,
    ) -> None:
        self.db, self.run, self.cliente, self.uid, self.ahora, self.limite = db, run, cliente, uid, ahora, limite
        self.prefijo = prefijo
        self.parcial = False
        self.motivo: str | None = None

    def agotado(self) -> bool:
        if self.ahora() < self.limite:
            return False
        self.parcial, self.motivo = True, "Caja de tiempo agotada."
        return True


async def _fases(ctx: _Ctx, hoy: date, presupuesto: int) -> None:
    db, run = ctx.db, ctx.run
    ultimo = await db.scalar(select(func.max(CatalogoBdns.sincronizado_at)))
    if ultimo is None or ctx.ahora() - ultimo > REFRESCO_CATALOGOS:
        try:
            await refrescar_catalogos(db, ctx.cliente, usuario_id=ctx.uid)
        except BdnsNoDisponible:
            run.errores += 1
            logger.warning("Sync BDNS: no se pudieron refrescar los catálogos.")
    resolvedor = await cargar_resolvedor(db)
    try:
        if "pagina" in (run.cursor or {}) and not await _fase_a(ctx):
            return
        if not await _fase_b(ctx, resolvedor):
            return
        if run.modo == "incremental":
            await _fase_c(ctx, resolvedor, hoy, presupuesto)
    finally:
        run.organos_sin_resolver = resolvedor.organos_sin_resolver
        await db.commit()


def _fin_trozo(trozo: date, modo: str, hasta: date) -> date:
    return hasta if modo != "backfill" else min(hasta, sumar_meses(trozo, 1) - timedelta(days=1))


async def _fase_a(ctx: _Ctx) -> bool:
    db, run = ctx.db, ctx.run
    cursor = dict(run.cursor or {})
    hasta = date.fromisoformat(cursor["hasta"])
    trozo, pagina = date.fromisoformat(cursor["trozo"]), int(cursor["pagina"])
    while trozo <= hasta:
        fin = _fin_trozo(trozo, run.modo, hasta)
        hay_mas = True
        while hay_mas:
            if ctx.agotado():
                return False
            resultado = await ctx.cliente.listar_pagina(trozo, fin, pagina=pagina)
            run.insertadas += await _guardar_resumen(db, resultado.convocatorias, ctx.uid, ctx.ahora())
            run.listadas += len(resultado.convocatorias)
            hay_mas = bool(resultado.convocatorias) and not resultado.ultima
            pagina = pagina + 1 if hay_mas else 0
            if not hay_mas:
                trozo = fin + timedelta(days=1)
            run.cursor = {**cursor, "trozo": trozo.isoformat(), "pagina": pagina}
            await db.commit()
    run.cursor = {"fase": "B"}
    await db.commit()
    return True


async def _guardar_resumen(db: AsyncSession, filas: list[ConvocatoriaBdns], uid: int, ahora: datetime) -> int:
    """Upsert del resumen del listado; devuelve las filas nuevas. Orden fijo por código (sin deadlocks)."""
    unicas = {f.codigo_bdns: f for f in filas if f.codigo_bdns}
    if not unicas:
        return 0
    valores = [
        {
            "codigo_bdns": codigo,
            "titulo": (f.titulo or codigo)[:500],
            "nivel_administracion": NIVEL_BDNS_A_NUESTRO.get((f.nivel1 or "").upper()),
            "administracion": f.nivel2[:200] if f.nivel2 else None,
            "organo_convocante": f.nivel3[:300] if f.nivel3 else None,
            "fecha_registro": f.fecha_registro,
            "financiada_mrr": f.financiada_mrr,
            "first_seen_at": ahora,
            "last_seen_at": ahora,
            "created_by": uid,
            "updated_by": uid,
        }
        for codigo, f in sorted(unicas.items())
    ]
    ins = insert(Convocatoria).values(valores)
    resultado: Any = await db.execute(
        ins.on_conflict_do_update(
            index_elements=[Convocatoria.codigo_bdns],
            set_={"last_seen_at": ins.excluded.last_seen_at, "removed_at": None},
        ).returning(literal_column("(xmax = 0)"))
    )
    return sum(1 for (nueva,) in resultado if nueva)


async def _fase_b(ctx: _Ctx, resolvedor: ResolvedorCatalogo) -> bool:
    ultimo_id: int | None = None
    while True:
        consulta = select(Convocatoria.id, Convocatoria.codigo_bdns).where(
            Convocatoria.detalle_sync_at.is_(None),
            Convocatoria.removed_at.is_(None),
            Convocatoria.codigo_bdns.like(f"{ctx.prefijo}%"),
        )
        if ultimo_id is not None:
            consulta = consulta.where(Convocatoria.id < ultimo_id)
        lote = (await ctx.db.execute(consulta.order_by(Convocatoria.id.desc()).limit(LOTE_B))).all()
        if not lote:
            return True
        for i, (_, codigo) in enumerate(lote):
            if ctx.agotado():
                return False
            await _refrescar(ctx, resolvedor, codigo)
            if i % COMMIT_CADA == COMMIT_CADA - 1:
                await ctx.db.commit()
        await ctx.db.commit()
        ultimo_id = lote[-1][0]


async def _fase_c(ctx: _Ctx, resolvedor: ResolvedorCatalogo, hoy: date, presupuesto: int) -> None:
    c = Convocatoria
    elegible = and_(
        c.removed_at.is_(None),
        c.codigo_bdns.like(f"{ctx.prefijo}%"),
        c.detalle_sync_at.is_not(None),
        c.detalle_sync_at < ctx.run.started_at,
        or_(
            c.fecha_fin_solicitud >= hoy,
            c.fecha_inicio_solicitud > hoy,
            and_(
                c.fecha_fin_solicitud.is_(None),
                or_(c.plazo_permanente, c.fecha_registro >= hoy - timedelta(days=DIAS_SIN_PLAZO_RECIENTE)),
            ),
        ),
    )
    codigos = (
        await ctx.db.scalars(
            select(c.codigo_bdns).where(elegible).order_by(c.detalle_sync_at.asc()).limit(presupuesto + 1)
        )
    ).all()
    if len(codigos) > presupuesto:
        ctx.parcial, ctx.motivo = True, "Presupuesto de refresco agotado."
        codigos = codigos[:presupuesto]
    for i, codigo in enumerate(codigos):
        if ctx.agotado():
            return
        await _refrescar(ctx, resolvedor, codigo)
        if i % COMMIT_CADA == COMMIT_CADA - 1:
            await ctx.db.commit()


async def _refrescar(ctx: _Ctx, resolvedor: ResolvedorCatalogo, codigo: str) -> None:
    """Detalle de una convocatoria: upsert si cambió, retirada suave si la BDNS ya no la tiene."""
    db, run, uid = ctx.db, ctx.run, ctx.uid
    try:
        detalle = await ctx.cliente.detalle(codigo)
    except BdnsNoDisponible:
        run.errores += 1
        return
    ahora = ctx.ahora()
    run.detalles += 1
    c = Convocatoria
    if detalle is None:
        await db.execute(
            update(c)
            .where(c.codigo_bdns == codigo, c.removed_at.is_(None))
            .values(removed_at=ahora, detalle_sync_at=ahora, updated_by=uid, updated_at=ahora)
        )
        run.retiradas += 1
        return
    try:
        fila = mapear_detalle(
            detalle,
            organo_ids=resolvedor.resolver_organo(ruta_organo(detalle)),
            region_ids=resolvedor.resolver_regiones(etiquetas_region(detalle)),
        )
    except DetalleInvalido:
        run.errores += 1
        return
    if fila["codigo_bdns"] != codigo:
        run.errores += 1
        return
    previo = await db.scalar(select(c.contenido_hash).where(c.codigo_bdns == codigo))
    if previo == fila["contenido_hash"]:
        await db.execute(
            update(c).where(c.codigo_bdns == codigo).values(detalle_sync_at=ahora, last_seen_at=ahora, removed_at=None)
        )
        return
    await _upsert_detalle(db, fila, ahora, uid)
    if previo is not None:
        run.actualizadas += 1


async def _upsert_detalle(db: AsyncSession, fila: dict[str, Any], ahora: datetime, uid: int) -> None:
    mrr = fila["financiada_mrr"]
    valores = {
        **fila,
        # NOT NULL: sin dato en una fila nueva vale `false`; en una existente se conserva (abajo).
        "financiada_mrr": bool(mrr),
        "first_seen_at": ahora,
        "last_seen_at": ahora,
        "detalle_sync_at": ahora,
        "disponible_at": ahora,
        "created_by": uid,
        "updated_by": uid,
    }
    ins = insert(Convocatoria).values(valores)
    omitir = {"codigo_bdns", "financiada_mrr", *CONSERVAR}
    set_: dict[str, Any] = {k: ins.excluded[k] for k in fila if k not in omitir}
    set_.update({k: func.coalesce(ins.excluded[k], getattr(Convocatoria, k)) for k in CONSERVAR})
    set_.update(
        financiada_mrr=func.coalesce(literal(mrr, Boolean), Convocatoria.financiada_mrr),
        detalle_sync_at=ahora,
        last_seen_at=ahora,
        removed_at=None,
        disponible_at=func.coalesce(Convocatoria.disponible_at, ahora),
        updated_by=uid,
        updated_at=ahora,
    )
    await db.execute(ins.on_conflict_do_update(index_elements=[Convocatoria.codigo_bdns], set_=set_))
