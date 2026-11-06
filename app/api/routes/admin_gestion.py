"""Excepciones a la regla "Subvfy gestiona digitalización e I+D" (solo admin, N1).

Una excepción fuerza `gestiona_subvfy` de una convocatoria en un sentido u otro; borrarla la devuelve a la regla.
"""

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select

from app.api.deps import AdminDep, DbDep, PaginacionDep
from app.models import Convocatoria, GestionExcepcion
from app.schemas.common import Pagina
from app.schemas.convocatoria import ExcepcionGestionRead, ExcepcionGestionUpsert
from app.services import clasificacion

router = APIRouter(prefix="/admin/gestion-excepciones", tags=["admin-gestion"])


async def _convocatoria_id(db: DbDep, codigo_bdns: str) -> int:
    conv_id = await db.scalar(select(Convocatoria.id).where(Convocatoria.codigo_bdns == codigo_bdns))
    if conv_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Convocatoria no encontrada.")
    return conv_id


async def _leer(db: DbDep, codigo_bdns: str) -> ExcepcionGestionRead:
    fila = (
        await db.execute(
            select(
                Convocatoria.codigo_bdns, Convocatoria.titulo, GestionExcepcion.gestiona, GestionExcepcion.motivo,
                GestionExcepcion.updated_at,
            )  # fmt: skip
            .join(GestionExcepcion, GestionExcepcion.convocatoria_id == Convocatoria.id)
            .where(Convocatoria.codigo_bdns == codigo_bdns)
        )
    ).one()
    return ExcepcionGestionRead.model_validate(fila, from_attributes=True)


@router.get("", response_model=Pagina[ExcepcionGestionRead], summary="Listar las excepciones")
async def listar(db: DbDep, _: AdminDep, paginacion: PaginacionDep) -> Pagina[ExcepcionGestionRead]:
    base = select(
        Convocatoria.codigo_bdns, Convocatoria.titulo, GestionExcepcion.gestiona, GestionExcepcion.motivo,
        GestionExcepcion.updated_at,
    ).join(GestionExcepcion, GestionExcepcion.convocatoria_id == Convocatoria.id)  # fmt: skip
    total = await db.scalar(select(func.count()).select_from(GestionExcepcion)) or 0
    filas = await db.execute(
        base.order_by(GestionExcepcion.updated_at.desc(), GestionExcepcion.id.desc())
        .offset(paginacion.offset)
        .limit(paginacion.size)
    )
    return Pagina(
        items=[ExcepcionGestionRead.model_validate(f, from_attributes=True) for f in filas],
        total=total,
        page=paginacion.page,
        size=paginacion.size,
    )


@router.put(
    "/{codigo_bdns}",
    response_model=ExcepcionGestionRead,
    summary="Forzar si Subvfy gestiona una convocatoria",
    responses={404: {"description": "Convocatoria desconocida."}},
)
async def fijar(codigo_bdns: str, datos: ExcepcionGestionUpsert, db: DbDep, actual: AdminDep) -> ExcepcionGestionRead:
    conv_id = await _convocatoria_id(db, codigo_bdns)
    await clasificacion.fijar_excepcion(
        db, convocatoria_id=conv_id, gestiona=datos.gestiona, motivo=datos.motivo, usuario_id=actual.id
    )
    await db.commit()
    return await _leer(db, codigo_bdns)


@router.delete(
    "/{codigo_bdns}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Quitar la excepción (vuelve a regir la regla)",
    responses={404: {"description": "Convocatoria desconocida o sin excepción."}},
)
async def quitar(codigo_bdns: str, db: DbDep, actual: AdminDep) -> Response:
    conv_id = await _convocatoria_id(db, codigo_bdns)
    if await db.scalar(select(GestionExcepcion.id).where(GestionExcepcion.convocatoria_id == conv_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="La convocatoria no tiene excepción.")
    await clasificacion.fijar_excepcion(
        db, convocatoria_id=conv_id, gestiona=None, motivo=None, usuario_id=actual.id
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
