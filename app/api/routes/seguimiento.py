"""Follow-up board (plan N5a/N5b). See `app.services.seguimiento` for the scope and the role rules."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import DbDep, UsuarioActualDep
from app.schemas.seguimiento import (
    EstadoSeguimiento,
    EventoRead,
    SeguimientoCreate,
    SeguimientoRead,
    SeguimientoResumen,
    SeguimientoUpdate,
    TareaCreate,
    TareaUpdate,
)
from app.services import seguimiento as servicio

router = APIRouter(prefix="/seguimiento", tags=["seguimiento"])

_404: dict[int | str, dict[str, Any]] = {404: {"description": "Seguimiento (o tarea) inexistente o de otra empresa."}}


@router.get("", response_model=list[SeguimientoRead], summary="Tablero de seguimiento de mi empresa")
async def listar(
    db: DbDep,
    actual: UsuarioActualDep,
    estado: Annotated[EstadoSeguimiento | None, Query(description="Solo una columna.")] = None,
    responsable_id: Annotated[int | None, Query(description="Solo las tarjetas de esa persona.")] = None,
) -> list[SeguimientoRead]:
    return await servicio.listar(db, actual, estado=estado, responsable_id=responsable_id)


@router.get("/resumen", response_model=SeguimientoResumen, summary="Embudo del seguimiento")
async def resumen(db: DbDep, actual: UsuarioActualDep) -> SeguimientoResumen:
    return await servicio.resumen(db, actual)


@router.post(
    "",
    response_model=SeguimientoRead,
    status_code=status.HTTP_201_CREATED,
    summary="Añadir una convocatoria al seguimiento",
    responses={409: {"description": "Ya estaba en el seguimiento: `{detail, item}` con la tarjeta existente."}},
)
async def crear(datos: SeguimientoCreate, db: DbDep, actual: UsuarioActualDep) -> SeguimientoRead | JSONResponse:
    item, nueva = await servicio.crear(db, actual, datos)
    if nueva:
        return item
    return JSONResponse(
        jsonable_encoder({"detail": f"La convocatoria {datos.codigo_bdns} ya está en el seguimiento.", "item": item}),
        status_code=status.HTTP_409_CONFLICT,
    )


@router.get("/{seguimiento_id}", response_model=SeguimientoRead, summary="Una tarjeta", responses=_404)
async def obtener(seguimiento_id: int, db: DbDep, actual: UsuarioActualDep) -> SeguimientoRead:
    return await servicio.leer(db, actual, seguimiento_id)


@router.patch("/{seguimiento_id}", response_model=SeguimientoRead, summary="Mover, asignar o resolver", responses=_404)
async def actualizar(
    seguimiento_id: int, datos: SeguimientoUpdate, db: DbDep, actual: UsuarioActualDep
) -> SeguimientoRead:
    return await servicio.actualizar(db, actual, seguimiento_id, datos)


@router.delete(
    "/{seguimiento_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar del seguimiento", responses=_404
)
async def eliminar(seguimiento_id: int, db: DbDep, actual: UsuarioActualDep) -> None:
    await servicio.eliminar(db, actual, seguimiento_id)


@router.get(
    "/{seguimiento_id}/eventos", response_model=list[EventoRead], summary="Historial de movimientos", responses=_404
)
async def eventos(seguimiento_id: int, db: DbDep, actual: UsuarioActualDep) -> list[EventoRead]:
    return [EventoRead.model_validate(e) for e in await servicio.eventos(db, actual, seguimiento_id)]


@router.post(
    "/{seguimiento_id}/tareas",
    response_model=SeguimientoRead,
    status_code=status.HTTP_201_CREATED,
    summary="Añadir una tarea o documento (devuelve la tarjeta)",
    responses=_404,
)
async def crear_tarea(
    seguimiento_id: int, datos: TareaCreate, db: DbDep, actual: UsuarioActualDep
) -> SeguimientoRead:
    return await servicio.crear_tarea(db, actual, seguimiento_id, datos)


@router.patch(
    "/{seguimiento_id}/tareas/{tarea_id}",
    response_model=SeguimientoRead,
    summary="Marcar o editar una tarea (devuelve la tarjeta)",
    responses=_404,
)
async def actualizar_tarea(
    seguimiento_id: int, tarea_id: int, datos: TareaUpdate, db: DbDep, actual: UsuarioActualDep
) -> SeguimientoRead:
    return await servicio.actualizar_tarea(db, actual, seguimiento_id, tarea_id, datos)


@router.delete(
    "/{seguimiento_id}/tareas/{tarea_id}",
    response_model=SeguimientoRead,
    summary="Quitar una tarea (devuelve la tarjeta)",
    responses=_404,
)
async def eliminar_tarea(seguimiento_id: int, tarea_id: int, db: DbDep, actual: UsuarioActualDep) -> SeguimientoRead:
    return await servicio.eliminar_tarea(db, actual, seguimiento_id, tarea_id)
