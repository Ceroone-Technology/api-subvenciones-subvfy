"""Deduplicación y registro de lo que encuentra el motor (Hito 4, tarea 3).

**El alcance de la deduplicación es la alerta**, no el usuario: las alertas son
personales e independientes, así que la misma convocatoria puede ser novedad
para dos alertas distintas, aunque sean de la misma persona.

**Qué significa "ya notificada"**: que exista su fila en
`alerta_ejecucion_convocatoria` para esa alerta. No hay columna `notificada`;
el registro es la marca.

**La garantía la da Postgres**, no este código: `UNIQUE(alerta_id,
convocatoria_id)` más `ON CONFLICT DO NOTHING ... RETURNING` hacen que lo
devuelto sea exactamente lo insertado. Dos ciclos simultáneos de la misma
alerta no pueden registrar la misma novedad dos veces.

**Todo el registro va en una transacción**, con un único commit al final: si
algo falla a mitad, no queda nada marcado como visto. Lo contrario perdería un
resultado para siempre, porque en el ciclo siguiente ya no aparecería como
nuevo.

**Y el fallo se registra aparte** (`registrar_fallo`): ese rollback se lleva
también la fila de `alerta_ejecucion`, así que dejar constancia del error
exige una transacción nueva, después del rollback. Sin ella la alerta no
dejaba rastro y se reintentaba en cada ciclo.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria
from app.services.bdns_consulta import NIVEL_A_TIPO_ADMINISTRACION
from app.services.busqueda_convocatorias import FiltrosBusqueda, condiciones

logger = logging.getLogger(__name__)

# Los niveles de la BDNS vienen en texto (`nivel1`) y hay que traducirlos a los
# valores del CHECK de `convocatoria`. Se deriva del mapa de la consulta para
# no mantener dos tablas de equivalencias.
NIVEL_BDNS_A_NUESTRO = {
    "ESTADO": "estado",
    "AUTONOMICA": "ccaa",
    "LOCAL": "local",
    "OTROS": "otros",
}
assert set(NIVEL_BDNS_A_NUESTRO.values()) == set(NIVEL_A_TIPO_ADMINISTRACION)


async def seleccionar_nuevas(
    db: AsyncSession, alerta_id: int, filtros: FiltrosBusqueda, desde: datetime
) -> dict[str, int]:
    """Convocatorias locales que son novedad para esta alerta: `{codigo_bdns: id}`.

    Una sola consulta sobre la tabla local, sin tocar la BDNS: cumplen los
    filtros compartidos de `busqueda_convocatorias`, su detalle completo llegó
    después de `desde` (`disponible_at`, que se fija una vez) y la alerta no las
    ha notificado nunca (NOT EXISTS sobre `alerta_ejecucion_convocatoria`).
    """
    ya_notificada = (
        select(AlertaEjecucionConvocatoria.id)
        .where(
            AlertaEjecucionConvocatoria.alerta_id == alerta_id,
            AlertaEjecucionConvocatoria.convocatoria_id == Convocatoria.id,
        )
        .exists()
    )
    filas = await db.execute(
        select(Convocatoria.codigo_bdns, Convocatoria.id)
        .where(*condiciones(filtros), Convocatoria.disponible_at > desde, ~ya_notificada)
        .order_by(Convocatoria.disponible_at, Convocatoria.id)
    )
    return {codigo: id_local for codigo, id_local in filas.all()}


# Tope de `detalle_error`. La columna es TEXT, pero esto se sirve por la API y
# un mensaje de error no necesita más.
MAX_DETALLE_ERROR = 500
# Con un exponente mayor el timedelta desbordaría antes de que lo frene el tope.
_MAX_EXPONENTE_ESPERA = 20


def espera_reintento(fallos_consecutivos: int) -> timedelta:
    """Cuánto esperar tras el n-ésimo fallo seguido: base × 2^(n-1), con tope.

    Con los valores por defecto: 15 min, 30 min, 1 h, 2 h… hasta 24 h.
    """
    base = timedelta(minutes=settings.alertas_reintento_base_minutos)
    tope = timedelta(hours=settings.alertas_reintento_max_horas)
    exponente = min(max(fallos_consecutivos - 1, 0), _MAX_EXPONENTE_ESPERA)
    return min(base * 2**exponente, tope)


def detalle_de_error(error: BaseException) -> str:
    """Texto de `alerta_ejecucion.detalle_error`: clase y primera línea del
    mensaje, truncado.

    **Nunca `str(error)` a secas**: en un `DBAPIError` de SQLAlchemy incluye la
    sentencia y sus parámetros, es decir, datos de la BDNS, y este campo se
    devuelve por la API. Se usa el mensaje de la excepción del driver
    (`orig`), que no los lleva. El prefijo distingue un fallo de ejecución de
    uno de envío, que también usará el estado `error`.
    """
    causa = error.orig if isinstance(error, DBAPIError) and error.orig is not None else error
    lineas = str(causa).strip().splitlines()
    mensaje = lineas[0].strip() if lineas else ""
    return f"[ejecución] {type(error).__name__}: {mensaje}"[:MAX_DETALLE_ERROR]


async def registrar_fallo(
    db: AsyncSession,
    alerta_id: int,
    error: BaseException,
    *,
    usuario_sistema_id: int,
    ahora: datetime,
) -> AlertaEjecucion | None:
    """Deja constancia de una ejecución fallida, en **su propia transacción**.

    Hay que llamarla **después** del `rollback` de la ejecución que falló: el
    rollback deshace todo lo que hubiera en la transacción, y esta es la que
    sobrevive. Escribe la fila `alerta_ejecucion` con estado `error` y su
    detalle, sube `fallos_consecutivos` y fija `proximo_reintento_at`.

    `ahora` es **el momento del fallo**, no el inicio del ciclo: un lote con
    la BDNS caída puede durar más que la primera espera, y contarla desde el
    inicio dejaría la marca vencida al escribirla.

    **No toca `ultima_ejecucion_at`**: es el `desde` de la consulta a la BDNS,
    y avanzarlo perdería lo publicado entre el fallo y el reintento.

    Devuelve `None` si la alerta ya no existe (la borraron durante el ciclo).
    """
    # El contador sube en SQL y no desde el objeto de la alerta: esa instancia
    # está desprendida de la sesión y puede llevar un valor viejo.
    fallos = (
        await db.execute(
            update(Alerta)
            .where(Alerta.id == alerta_id)
            .values(
                fallos_consecutivos=Alerta.fallos_consecutivos + 1,
                updated_at=func.now(),
                updated_by=usuario_sistema_id,
            )
            .returning(Alerta.fallos_consecutivos)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if fallos is None:
        await db.rollback()
        return None

    await db.execute(
        update(Alerta)
        .where(Alerta.id == alerta_id)
        .values(proximo_reintento_at=ahora + espera_reintento(fallos))
        .execution_options(synchronize_session=False)
    )
    ejecucion = AlertaEjecucion(
        alerta_id=alerta_id,
        convocatorias_encontradas=0,
        estado_envio="error",
        detalle_error=detalle_de_error(error),
        created_by=usuario_sistema_id,
        updated_by=usuario_sistema_id,
    )
    db.add(ejecucion)
    await db.commit()
    return ejecucion


async def registrar_ejecucion(
    db: AsyncSession,
    alerta: Alerta,
    ids_por_codigo: dict[str, int],
    *,
    usuario_sistema_id: int,
) -> AlertaEjecucion:
    """Deja constancia de la ejecución y de sus novedades, y las devuelve ya
    registradas.

    `estado_envio` queda en `pendiente_envio` cuando hay novedades: el aviso lo
    manda otra funcionalidad, y decir `enviado` aquí sería falso.
    """
    ejecucion = AlertaEjecucion(
        alerta_id=alerta.id,
        convocatorias_encontradas=0,
        estado_envio="sin_novedades",
        created_by=usuario_sistema_id,
        updated_by=usuario_sistema_id,
    )
    db.add(ejecucion)
    await db.flush()  # para tener ejecucion.id

    if ids_por_codigo:
        registradas = await _marcar_notificadas(
            db, alerta.id, ejecucion.id, ids_por_codigo, usuario_sistema_id
        )
        # Lo que se guarda es lo realmente registrado, no el tamaño del lote:
        # si otro ciclo se adelantó con alguna, esa no es novedad de esta
        # ejecución.
        ejecucion.convocatorias_encontradas = len(registradas)
        if registradas:
            ejecucion.estado_envio = "pendiente_envio"

    await db.commit()
    return ejecucion


async def _marcar_notificadas(
    db: AsyncSession,
    alerta_id: int,
    ejecucion_id: int,
    ids_por_codigo: dict[str, int],
    usuario_sistema_id: int,
) -> list[int]:
    """Registra los pares alerta/convocatoria y devuelve los realmente
    insertados.

    `ON CONFLICT DO NOTHING` sobre `uq_alerta_convocatoria_notificada`: si otro
    ciclo se adelantó con la misma convocatoria, aquí no se duplica ni se
    cuenta como novedad.
    """
    sentencia = (
        insert_postgresql(AlertaEjecucionConvocatoria)
        .values(
            [
                {
                    "alerta_ejecucion_id": ejecucion_id,
                    "alerta_id": alerta_id,
                    "convocatoria_id": convocatoria_id,
                    "created_by": usuario_sistema_id,
                    "updated_by": usuario_sistema_id,
                }
                for convocatoria_id in ids_por_codigo.values()
            ]
        )
        .on_conflict_do_nothing(constraint="uq_alerta_convocatoria_notificada")
        .returning(AlertaEjecucionConvocatoria.convocatoria_id)
    )
    registradas = list(await db.scalars(sentencia))
    if len(registradas) != len(ids_por_codigo):
        logger.info(
            "Alerta %s: %d de %d novedades ya estaban registradas por otra ejecución.",
            alerta_id,
            len(ids_por_codigo) - len(registradas),
            len(ids_por_codigo),
        )
    return registradas
