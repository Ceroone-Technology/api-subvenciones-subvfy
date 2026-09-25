"""Motor de ejecución de alertas (Hito 4) — tareas 1 y 2: ciclo y consulta.

Qué hace hoy: elegir las alertas **activas que toca evaluar** según su
frecuencia, recorrerlas consultando la BDNS con sus criterios (tarea 2) y
dejar `ultima_ejecucion_at` marcada. Lo que todavía no hace, porque es de la
tarea 3: registrar los resultados en `alerta_ejecucion` y deduplicar lo ya
notificado.

**Sin APScheduler ni FastAPI dentro.** Recibe la sesión de fuera, así que
sirve igual desde un test, una CLI o la Lambda del Hito 7; `app/core/scheduler.py`
es solo quien lo llama cada cierto tiempo en local.

**Cómo se decide que una alerta toca**: `alerta.ultima_ejecucion_at` (que ya
existía en el esquema) contra el intervalo de su frecuencia, en **una sola
consulta**. No se deriva de `alerta_ejecucion`, que sería un MAX por alerta, y
no hay columna de próxima ejecución que mantener.

**Aislamiento**: si una alerta revienta, se registra con traza y se sigue con
las demás. Su `ultima_ejecucion_at` **no** avanza, así que el siguiente ciclo
la reintenta.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Alerta, AlertaOrgano, AlertaRegion
from app.services.bdns_cliente import ClienteBdns
from app.services.bdns_consulta import construir_consulta

logger = logging.getLogger(__name__)

# Cada cuánto vuelve a tocar una alerta, según su frecuencia. "inmediata" es
# intervalo cero: toca en cada ciclo, así que su cadencia real es el intervalo
# con el que se llame al motor (en local, el del scheduler).
INTERVALOS: dict[str, timedelta] = {
    "inmediata": timedelta(0),
    "diaria": timedelta(days=1),
    "semanal": timedelta(days=7),
}

# Una alerta que evalúa la BDNS y notifica no es gratis: se procesa por lotes
# para que un ciclo no se eternice si hay un atasco. Las que no entren salen
# en el ciclo siguiente, porque su ultima_ejecucion_at no ha avanzado.
MAX_POR_CICLO = 200

Evaluador = Callable[[AsyncSession, Alerta], Awaitable[None]]


@dataclass(frozen=True)
class ResumenCiclo:
    """Lo que hizo un ciclo. Se devuelve para poder loguearlo y comprobarlo."""

    pendientes: int
    evaluadas: int
    fallidas: int


async def evaluar_alerta(db: AsyncSession, alerta: Alerta) -> None:
    """Consulta la BDNS con los criterios de la alerta (tarea 2).

    Lo que **todavía no hace** es registrar el resultado en `alerta_ejecucion`
    ni deduplicar lo ya notificado: eso es la tarea 3. De momento los
    resultados se cuentan en el log y se descartan.

    Si los criterios no dan una consulta útil (`CriteriosAlertaInvalidos`) o la
    BDNS no responde (`BdnsNoDisponible`), la excepción sube al ciclo, que la
    registra y sigue con las demás alertas.
    """
    organos, regiones = await filtros_de_alerta(db, alerta.id)
    consulta = construir_consulta(alerta, organos, regiones, desde=_desde_de(alerta))

    async with ClienteBdns() as cliente:
        convocatorias = await cliente.buscar_todo(consulta)

    logger.info(
        "Alerta %s: %d convocatoria(s) encontradas en la BDNS (registro pendiente de la tarea 3).",
        alerta.id,
        len(convocatorias),
    )


async def filtros_de_alerta(db: AsyncSession, alerta_id: int) -> tuple[list[int], list[int]]:
    """Los ids BDNS de órgano y región de una alerta, en dos consultas.

    Se leen aquí y no en la selección del ciclo a propósito: la mayoría de los
    ciclos no traen ninguna alerta, y cargar los filtros de todas por si acaso
    sería trabajo tirado.
    """
    organos = await db.scalars(
        select(AlertaOrgano.organo_bdns_id).where(AlertaOrgano.alerta_id == alerta_id)
    )
    regiones = await db.scalars(
        select(AlertaRegion.region_bdns_id).where(AlertaRegion.alerta_id == alerta_id)
    )
    return list(organos), list(regiones)


def _desde_de(alerta: Alerta) -> date:
    """Desde cuándo se pregunta a la BDNS.

    Lo normal es "desde la última vez que se evaluó". La primera vez no hay
    referencia, así que se usa una ventana corta configurable en vez de
    arrastrar el histórico entero.
    """
    if alerta.ultima_ejecucion_at is not None:
        return alerta.ultima_ejecucion_at.date()
    return (datetime.now(UTC) - timedelta(days=settings.bdns_dias_primera_ejecucion)).date()


async def alertas_pendientes(
    db: AsyncSession, *, ahora: datetime | None = None, limite: int = MAX_POR_CICLO
) -> Sequence[Alerta]:
    """Alertas activas a las que toca evaluarse, las más atrasadas primero."""
    ahora = ahora or datetime.now(UTC)
    vencidas = [
        and_(Alerta.frecuencia == frecuencia, Alerta.ultima_ejecucion_at <= ahora - intervalo)
        for frecuencia, intervalo in INTERVALOS.items()
    ]
    alertas = (
        await db.scalars(
            select(Alerta)
            .where(
                Alerta.activa.is_(True),
                # Nunca ejecutada, o vencida según su frecuencia.
                or_(Alerta.ultima_ejecucion_at.is_(None), *vencidas),
            )
            .order_by(Alerta.ultima_ejecucion_at.asc().nulls_first(), Alerta.id)
            .limit(limite)
        )
    ).all()
    for alerta in alertas:
        # Se desprenden de la sesión con todas sus columnas ya cargadas. Si
        # siguieran atadas, el rollback que aísla el fallo de una alerta
        # expiraría los atributos de las demás, y leer la siguiente del bucle
        # dispararía una recarga perezosa (MissingGreenlet en async).
        db.expunge(alerta)
    return alertas


async def procesar_alertas_pendientes(
    db: AsyncSession,
    *,
    usuario_sistema_id: int,
    evaluador: Evaluador | None = None,
    ahora: datetime | None = None,
) -> ResumenCiclo:
    """Un ciclo del motor: selecciona, evalúa una a una y marca lo hecho.

    `evaluador` existe para que los tests puedan inyectar un doble; en
    producción es `evaluar_alerta`.
    """
    evaluar = evaluador or evaluar_alerta
    ahora = ahora or datetime.now(UTC)
    pendientes = await alertas_pendientes(db, ahora=ahora)
    logger.info("Motor de alertas: %d alerta(s) pendientes.", len(pendientes))

    evaluadas = 0
    fallidas = 0
    for alerta in pendientes:
        try:
            await evaluar(db, alerta)
        except Exception:
            # Aislamiento entre alertas: el fallo de una no puede tumbar el
            # ciclo. Se loguea con traza y con el id (nada de datos del
            # usuario), y se deja la sesión limpia para la siguiente.
            fallidas += 1
            logger.exception("Motor de alertas: falló la alerta %s; se continúa con las demás.", alerta.id)
            await db.rollback()
            continue
        await _marcar_ejecutada(db, alerta.id, usuario_sistema_id, ahora)
        evaluadas += 1

    resumen = ResumenCiclo(pendientes=len(pendientes), evaluadas=evaluadas, fallidas=fallidas)
    logger.info(
        "Motor de alertas: ciclo terminado (%d evaluadas, %d con error).", resumen.evaluadas, resumen.fallidas
    )
    return resumen


async def _marcar_ejecutada(
    db: AsyncSession, alerta_id: int, usuario_sistema_id: int, ahora: datetime
) -> None:
    """Commit por alerta: si el ciclo se corta a la mitad, lo ya hecho no se
    repite. Firma con el usuario de sistema, como el resto de procesos
    automáticos."""
    await db.execute(
        update(Alerta)
        .where(Alerta.id == alerta_id)
        .values(ultima_ejecucion_at=ahora, updated_at=func.now(), updated_by=usuario_sistema_id)
        # Sin sincronizar la sesión: por defecto este UPDATE expira los
        # atributos de las alertas ya cargadas, y leer la siguiente del bucle
        # dispararía una recarga perezosa que en async revienta
        # (MissingGreenlet). Aquí no hace falta: los objetos no se releen.
        .execution_options(synchronize_session=False)
    )
    await db.commit()
