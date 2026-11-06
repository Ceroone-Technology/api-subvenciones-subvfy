"""Discards ("No me interesa", N4): personal, addressed by `codigo_bdns` like favorites.

`no_descartada` is the filter every feed of suggestions (radar, news) applies so a hidden grant stays hidden.
"""

from sqlalchemy import ColumnElement, delete, exists, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Convocatoria, Descarte


class ConvocatoriaDesconocida(Exception):
    """The code is not in the local table."""


class DescarteInexistente(Exception):
    """The user has not discarded that grant."""


def no_descartada(usuario_id: int) -> ColumnElement[bool]:
    """SQL condition: the grant (`convocatoria.id`) is not discarded by `usuario_id`."""
    return ~exists().where(Descarte.usuario_id == usuario_id, Descarte.convocatoria_id == Convocatoria.id)


async def descartar(db: AsyncSession, usuario_id: int, codigo_bdns: str) -> tuple[Descarte, bool]:
    """Hide a grant. Idempotent: `(descarte, creado)`, with `creado` False when it was already hidden."""
    convocatoria_id = await db.scalar(select(Convocatoria.id).where(Convocatoria.codigo_bdns == codigo_bdns))
    if convocatoria_id is None:
        raise ConvocatoriaDesconocida(codigo_bdns)
    # ON CONFLICT DO NOTHING: two concurrent clicks must not turn one of them into a 500.
    insertado = await db.scalar(
        insert_postgresql(Descarte)
        .values(usuario_id=usuario_id, convocatoria_id=convocatoria_id, created_by=usuario_id, updated_by=usuario_id)
        .on_conflict_do_nothing(index_elements=[Descarte.usuario_id, Descarte.convocatoria_id])
        .returning(Descarte.id)
    )
    await db.commit()
    descarte = await db.scalar(
        select(Descarte).where(Descarte.usuario_id == usuario_id, Descarte.convocatoria_id == convocatoria_id)
    )
    assert descarte is not None
    return descarte, insertado is not None


async def recuperar(db: AsyncSession, usuario_id: int, codigo_bdns: str) -> None:
    """Show a grant again. Raises `DescarteInexistente` when the user had not discarded it."""
    convocatoria_id = select(Convocatoria.id).where(Convocatoria.codigo_bdns == codigo_bdns).scalar_subquery()
    borrados = await db.execute(
        delete(Descarte)
        .where(Descarte.usuario_id == usuario_id, Descarte.convocatoria_id == convocatoria_id)
        .returning(Descarte.id)
    )
    if borrados.first() is None:
        raise DescarteInexistente(codigo_bdns)
    await db.commit()
