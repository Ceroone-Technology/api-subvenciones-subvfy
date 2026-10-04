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
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria
from app.services.bdns_cliente import ConvocatoriaBdns
from app.services.bdns_consulta import NIVEL_A_TIPO_ADMINISTRACION

logger = logging.getLogger(__name__)

# Los niveles de la BDNS vienen en texto (`nivel1`) y hay que traducirlos a los
# valores del CHECK de `convocatoria`. Se deriva del mapa de la consulta para
# no mantener dos tablas de equivalencias.
def _limite(columna: str) -> int:
    """Longitud máxima de una columna de texto de `convocatoria`.

    Se lee del modelo y no se escribe a mano: si mañana cambia el esquema, el
    recorte cambia con él y no hay dos verdades.
    """
    tipo = Convocatoria.__table__.c[columna].type
    longitud = getattr(tipo, "length", None)
    if longitud is None:  # pragma: no cover - solo si alguien quita el String(n)
        raise RuntimeError(f"La columna convocatoria.{columna} ya no tiene longitud máxima.")
    return int(longitud)


NIVEL_BDNS_A_NUESTRO = {
    "ESTADO": "estado",
    "AUTONOMICA": "ccaa",
    "LOCAL": "local",
    "OTROS": "otros",
}
assert set(NIVEL_BDNS_A_NUESTRO.values()) == set(NIVEL_A_TIPO_ADMINISTRACION)


async def filtrar_nuevas(
    db: AsyncSession, alerta_id: int, convocatorias: Sequence[ConvocatoriaBdns]
) -> list[ConvocatoriaBdns]:
    """Las que esta alerta no ha notificado nunca, en una sola consulta.

    Antes de comparar con lo ya notificado se descarta lo que no se puede
    cachear: sin `codigo_bdns` (o con uno que no cabe en la columna) no hay con
    qué identificarla, y **sin título tampoco vale**, porque `convocatoria.titulo` es NOT NULL y rellenarlo con
    un texto de relleno machacaría el título real que hubiera guardado un
    favorito (ver el upsert de abajo). El descarte va **antes** de la
    deduplicación a propósito: así una convocatoria descartada no queda
    marcada como vista y vuelve a entrar si la BDNS la publica completa.

    También deduplica **dentro del lote**: la BDNS puede repetir un código
    entre páginas, y sin esto el `INSERT` chocaría consigo mismo.
    """
    del_lote: dict[str, ConvocatoriaBdns] = {}
    for convocatoria in convocatorias:
        if not convocatoria.codigo_bdns:
            logger.warning("BDNS: convocatoria sin código, descartada (id %s).", convocatoria.id_bdns)
            continue
        if len(convocatoria.codigo_bdns) > _limite("codigo_bdns"):
            # Este no se recorta: el código es la **identidad** (clave del
            # ON CONFLICT, y lo que usan el frontend y los favoritos para
            # emparejar). Truncarlo inventaría una convocatoria o pisaría otra.
            logger.warning(
                "BDNS: código %r demasiado largo (%d caracteres), descartada.",
                convocatoria.codigo_bdns,
                len(convocatoria.codigo_bdns),
            )
            continue
        if not (convocatoria.titulo or "").strip():
            # Se avisa en vez de descartar en silencio: si la BDNS empieza a
            # devolver fichas sin título, esto es lo que lo delata. Como no
            # queda registrada, el aviso se repetirá en cada ciclo mientras
            # siga llegando así.
            logger.warning(
                "BDNS: convocatoria %s sin título, descartada (no se puede cachear).",
                convocatoria.codigo_bdns,
            )
            continue
        if convocatoria.codigo_bdns not in del_lote:
            del_lote[convocatoria.codigo_bdns] = convocatoria
    if not del_lote:
        return []

    ya_notificadas = set(
        await db.scalars(
            select(Convocatoria.codigo_bdns)
            .join(
                AlertaEjecucionConvocatoria,
                AlertaEjecucionConvocatoria.convocatoria_id == Convocatoria.id,
            )
            .where(
                AlertaEjecucionConvocatoria.alerta_id == alerta_id,
                Convocatoria.codigo_bdns.in_(del_lote),
            )
        )
    )
    return [
        convocatoria for codigo, convocatoria in del_lote.items() if codigo not in ya_notificadas
    ]


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
    nuevas: Sequence[ConvocatoriaBdns],
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

    if nuevas:
        ids_por_codigo = await _cachear_convocatorias(db, nuevas, usuario_sistema_id)
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


def _recortar(valor: str | None, limite: int, *, codigo_bdns: str | None = None, campo: str = "") -> str | None:
    """Recorta a la longitud de la columna, y trata la cadena vacía como nula.

    Lo segundo no es un descuido: `""` sobreviviría al `COALESCE` del upsert y
    machacaría con nada un valor ya cacheado. Devolviendo `None`, se conserva.

    Postgres **no recorta**: aborta la sentencia. Y como el registro va en una
    sola transacción, ese error se llevaría por delante la fila de
    `alerta_ejecucion` y haría fallar la ejecución entera (que el motor
    registra y reintenta con espera, pero sin novedades mientras dure). De ahí
    que se recorte aquí.
    """
    if not valor:
        return None
    if len(valor) > limite:
        # Se pierde texto de la fuente: que se vea. Sale una vez por
        # convocatoria, porque después la dedup ya la salta.
        logger.warning(
            "BDNS: %s de la convocatoria %s recortado de %d a %d caracteres.",
            campo or "campo",
            codigo_bdns,
            len(valor),
            limite,
        )
        return valor[:limite]
    return valor


async def _cachear_convocatorias(
    db: AsyncSession, convocatorias: Sequence[ConvocatoriaBdns], usuario_sistema_id: int
) -> dict[str, int]:
    """Upsert masivo en la caché local, con el patrón de `favoritos.py`.

    La regla de allí —un guardado parcial no borra lo ya cacheado— se traduce
    al lote con `COALESCE(excluded.campo, convocatoria.campo)`: si la BDNS
    devuelve un campo vacío, se conserva lo que hubiera (por ejemplo, la ficha
    completa que guardó un favorito).

    De ahí que aquí **no haya respaldos en Python** (`x or y`) para los campos
    que entran en ese COALESCE: sustituir un `None` antes del INSERT deja a
    `excluded.campo` con valor, el COALESCE se vuelve inútil y el respaldo
    machaca el dato cacheado. Si añades un campo, o va sin respaldo, o queda
    fuera del COALESCE a sabiendas.

    `financiada_mrr` sí se sobrescribe siempre: es el único campo fuera del
    COALESCE, porque la BDNS manda `mrr` en todas las filas y para un booleano
    vale lo último que diga la fuente.

    Y un detalle de Postgres: para `titulo`, que es NOT NULL, **el COALESCE no
    llega a actuar nunca**. El NOT NULL se comprueba sobre la fila propuesta
    antes de resolver el conflicto, así que un título nulo revienta el INSERT
    aunque la fila ya exista. Quien protege la caché es el descarte de
    `filtrar_nuevas`; el COALESCE se queda como red por coherencia con el resto
    de campos. Lo fija `test_el_upsert_rechaza_un_titulo_nulo`.
    """
    filas = [
        {
            "codigo_bdns": convocatoria.codigo_bdns,
            "titulo": _recortar(
                convocatoria.titulo, _limite("titulo"), codigo_bdns=convocatoria.codigo_bdns, campo="título"
            ),
            # nivel_administracion no se recorta: su valor sale de nuestro mapa
            # (estado/ccaa/local/otros) o es None, así que nunca pasa de 20.
            "nivel_administracion": NIVEL_BDNS_A_NUESTRO.get((convocatoria.nivel1 or "").upper()),
            "administracion": _recortar(
                convocatoria.nivel2,
                _limite("administracion"),
                codigo_bdns=convocatoria.codigo_bdns,
                campo="administración",
            ),
            # Sin respaldo a nivel2: si nivel3 viene vacío, lo que toca es
            # dejar que el COALESCE conserve el órgano cacheado, que es más
            # específico, en vez de sustituirlo por la administración.
            "organo_convocante": _recortar(
                convocatoria.nivel3,
                _limite("organo_convocante"),
                codigo_bdns=convocatoria.codigo_bdns,
                campo="órgano convocante",
            ),
            "fecha_registro": convocatoria.fecha_registro,
            # Único campo fuera del COALESCE, y a propósito: la BDNS envía
            # `mrr` en todas las filas, así que un False no es "dato ausente"
            # sino una corrección real que debe pisar lo cacheado. No le pongas
            # COALESCE ni hagas anulable el campo del dataclass.
            "financiada_mrr": convocatoria.financiada_mrr,
            "created_by": usuario_sistema_id,
            "updated_by": usuario_sistema_id,
        }
        for convocatoria in convocatorias
    ]
    insercion = insert_postgresql(Convocatoria).values(filas)
    conservando = {
        campo: func.coalesce(insercion.excluded[campo], getattr(Convocatoria, campo))
        for campo in ("titulo", "nivel_administracion", "administracion", "organo_convocante", "fecha_registro")
    }
    sentencia = insercion.on_conflict_do_update(
        index_elements=[Convocatoria.codigo_bdns],
        set_={
            **conservando,
            "financiada_mrr": insercion.excluded.financiada_mrr,
            "sincronizado_at": func.now(),
            "updated_at": func.now(),
            "updated_by": usuario_sistema_id,
        },
    ).returning(Convocatoria.id, Convocatoria.codigo_bdns)
    return {codigo: id_local for id_local, codigo in (await db.execute(sentencia)).all()}


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
