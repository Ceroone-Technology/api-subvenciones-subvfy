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
"""

import logging
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria
from app.services.bdns_cliente import ConvocatoriaBdns
from app.services.bdns_consulta import NIVEL_A_TIPO_ADMINISTRACION

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


async def filtrar_nuevas(
    db: AsyncSession, alerta_id: int, convocatorias: Sequence[ConvocatoriaBdns]
) -> list[ConvocatoriaBdns]:
    """Las que esta alerta no ha notificado nunca, en una sola consulta.

    También deduplica **dentro del lote**: la BDNS puede repetir un código
    entre páginas, y sin esto el `INSERT` chocaría consigo mismo.
    """
    del_lote: dict[str, ConvocatoriaBdns] = {}
    for convocatoria in convocatorias:
        if convocatoria.codigo_bdns and convocatoria.codigo_bdns not in del_lote:
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


async def _cachear_convocatorias(
    db: AsyncSession, convocatorias: Sequence[ConvocatoriaBdns], usuario_sistema_id: int
) -> dict[str, int]:
    """Upsert masivo en la caché local, con el patrón de `favoritos.py`.

    La regla de allí —un guardado parcial no borra lo ya cacheado— se traduce
    al lote con `COALESCE(excluded.campo, convocatoria.campo)`: si la BDNS
    devuelve un campo vacío, se conserva lo que hubiera (por ejemplo, la ficha
    completa que guardó un favorito).
    """
    filas = [
        {
            "codigo_bdns": convocatoria.codigo_bdns,
            "titulo": convocatoria.titulo or "(sin título)",
            "nivel_administracion": NIVEL_BDNS_A_NUESTRO.get((convocatoria.nivel1 or "").upper()),
            "administracion": convocatoria.nivel2,
            "organo_convocante": convocatoria.nivel3 or convocatoria.nivel2,
            "fecha_registro": convocatoria.fecha_registro,
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
