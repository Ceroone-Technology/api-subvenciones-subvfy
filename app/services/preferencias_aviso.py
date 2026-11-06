"""Notice preferences of the signed-in user (plan N9a).

Always the five kinds, in display order: the ones the user never saved come with the defaults of the design, so a
new user needs no setup. `PUT` replaces the kinds it carries and leaves the rest as they were.
"""

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PreferenciaAviso
from app.models.preferencia_aviso import PREFERENCIAS_POR_DEFECTO, TIPOS_AVISO
from app.schemas.preferencia_aviso import PreferenciaAvisoItem


async def obtener(db: AsyncSession, usuario_id: int) -> list[PreferenciaAvisoItem]:
    guardadas = {
        p.tipo: p
        for p in await db.scalars(
            select(PreferenciaAviso)
            .where(PreferenciaAviso.usuario_id == usuario_id)
            .execution_options(populate_existing=True)
        )
    }
    resultado: list[PreferenciaAvisoItem] = []
    for tipo in TIPOS_AVISO:
        fila = guardadas.get(tipo)
        email, app = (fila.email, fila.app) if fila is not None else PREFERENCIAS_POR_DEFECTO[tipo]
        resultado.append(PreferenciaAvisoItem(tipo=tipo, email=email, app=app))
    return resultado


async def guardar(
    db: AsyncSession, usuario_id: int, preferencias: list[PreferenciaAvisoItem]
) -> list[PreferenciaAvisoItem]:
    tipos = [p.tipo for p in preferencias]
    if len(set(tipos)) != len(tipos):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Cada tipo de aviso solo puede aparecer una vez."
        )
    for p in preferencias:
        valores = {"email": p.email, "app": p.app}
        await db.execute(
            insert_postgresql(PreferenciaAviso)
            .values(usuario_id=usuario_id, tipo=p.tipo, created_by=usuario_id, updated_by=usuario_id, **valores)
            .on_conflict_do_update(
                index_elements=[PreferenciaAviso.usuario_id, PreferenciaAviso.tipo],
                set_={**valores, "updated_by": usuario_id, "updated_at": func.now()},
            )
        )
    await db.commit()
    return await obtener(db, usuario_id)
