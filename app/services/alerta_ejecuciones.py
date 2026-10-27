"""Historial de ejecuciones de una alerta (Hito 4): solo lectura.

Las filas las escribirá el motor de alertas, que aún no existe; aquí solo se
consultan.

**El aislamiento es el de la alerta**: una ejecución no se pide sin decir de
qué alerta es, y esa alerta tiene que ser del usuario del token. Si no lo es,
la respuesta es la misma que si no existiera (404), igual que en el resto de
las alertas.
"""

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria
from app.schemas.alerta_ejecucion import EjecucionDetalle, EjecucionRead
from app.schemas.convocatoria import ConvocatoriaRead
from app.services.alertas import AlertaNoEncontrada


class EjecucionNoEncontrada(Exception):
    """No existe, o no es de esa alerta: a propósito, son indistinguibles."""


async def listar_ejecuciones(
    db: AsyncSession,
    alerta_id: int,
    usuario_id: int,
    *,
    offset: int,
    limit: int,
    estado_envio: str | None = None,
) -> tuple[list[EjecucionRead], int]:
    """Página del historial de una alerta, la ejecución más reciente primero.

    Devuelve solo el resumen: sin tocar `alerta_ejecucion_convocatoria`, aquí
    no hay N+1 posible.
    """
    await _exigir_alerta_propia(db, alerta_id, usuario_id)

    filtros: list[ColumnElement[bool]] = [AlertaEjecucion.alerta_id == alerta_id]
    if estado_envio is not None:
        filtros.append(AlertaEjecucion.estado_envio == estado_envio)

    total = await db.scalar(select(func.count()).select_from(AlertaEjecucion).where(*filtros))
    ejecuciones = (
        await db.scalars(
            select(AlertaEjecucion)
            .where(*filtros)
            # fecha_ejecucion_at y no created_at: es la fecha del dominio, y
            # además es la que indexa ix_alerta_ejecucion_alerta junto con
            # alerta_id. El id desempata, como en el listado de alertas.
            .order_by(AlertaEjecucion.fecha_ejecucion_at.desc(), AlertaEjecucion.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [EjecucionRead.model_validate(ejecucion) for ejecucion in ejecuciones], total or 0


async def obtener_ejecucion(
    db: AsyncSession, alerta_id: int, ejecucion_id: int, usuario_id: int
) -> EjecucionDetalle:
    """El resumen más las convocatorias detectadas, en dos consultas.

    El `JOIN alerta` mete la propiedad en la misma condición que el id: así no
    hay forma de leer la ejecución de una alerta ajena, ni la de otra alerta
    del propio usuario.
    """
    ejecucion = (
        await db.execute(
            select(AlertaEjecucion)
            .join(Alerta, Alerta.id == AlertaEjecucion.alerta_id)
            .where(
                AlertaEjecucion.id == ejecucion_id,
                AlertaEjecucion.alerta_id == alerta_id,
                Alerta.usuario_id == usuario_id,
            )
        )
    ).scalar_one_or_none()
    if ejecucion is None:
        raise EjecucionNoEncontrada

    convocatorias = (
        await db.scalars(
            select(Convocatoria)
            .join(
                AlertaEjecucionConvocatoria,
                AlertaEjecucionConvocatoria.convocatoria_id == Convocatoria.id,
            )
            .where(AlertaEjecucionConvocatoria.alerta_ejecucion_id == ejecucion_id)
            # En el orden en que se registraron durante la ejecución.
            .order_by(AlertaEjecucionConvocatoria.id)
        )
    ).all()

    return EjecucionDetalle(
        **EjecucionRead.model_validate(ejecucion).model_dump(),
        convocatorias=[ConvocatoriaRead.model_validate(c) for c in convocatorias],
    )


async def _exigir_alerta_propia(db: AsyncSession, alerta_id: int, usuario_id: int) -> None:
    propia = await db.scalar(
        select(Alerta.id).where(Alerta.id == alerta_id, Alerta.usuario_id == usuario_id)
    )
    if propia is None:
        raise AlertaNoEncontrada
