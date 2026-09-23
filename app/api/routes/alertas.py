"""Alertas del usuario (Hito 4): listado, detalle, mutaciones e historial.

El router solo valida la entrada (schemas) y traduce a HTTP los errores del
servicio; la lógica está en `app.services.alertas` y, para el historial de
ejecuciones, en `app.services.alerta_ejecuciones`.

El historial cuelga de la alerta (`/alertas/{id}/ejecuciones`) y no es un
recurso aparte: una ejecución no significa nada sin su alerta, y así el
control de propiedad sale de la misma comprobación.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbDep, PaginacionDep, UsuarioActualDep
from app.schemas.alerta import AlertaCreate, AlertaRead, AlertaUpdate
from app.schemas.alerta_ejecucion import EjecucionDetalle, EjecucionRead, EstadoEnvio
from app.schemas.common import Pagina
from app.services import alerta_ejecuciones as servicio_ejecuciones
from app.services import alertas as servicio

router = APIRouter(prefix="/alertas", tags=["alertas"])

_NO_ENCONTRADA = HTTPException(status.HTTP_404_NOT_FOUND, detail="Alerta no encontrada.")
_NO_ENCONTRADA_EJECUCION = HTTPException(status.HTTP_404_NOT_FOUND, detail="Ejecución no encontrada.")
# Declarado para que el OpenAPI que consume el frontend documente el 404.
# Es el mismo para una alerta inexistente y para una ajena, a propósito.
_RESPUESTA_404: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "La alerta no existe o no es tuya."}
}


@router.get("", response_model=Pagina[AlertaRead], summary="Listar mis alertas")
async def listar_alertas(
    db: DbDep,
    paginacion: PaginacionDep,
    actual: UsuarioActualDep,
    organo_id: Annotated[
        int | None, Query(gt=0, le=2_147_483_647, description="Solo las alertas que incluyen este órgano (id BDNS).")
    ] = None,
    region_id: Annotated[
        int | None, Query(gt=0, le=2_147_483_647, description="Solo las alertas que incluyen esta región (id BDNS).")
    ] = None,
    activa: Annotated[
        bool | None, Query(description="true: solo activas; false: solo pausadas. Sin él, todas.")
    ] = None,
) -> Pagina[AlertaRead]:
    """Más recientes primero. Siempre las del usuario del token, nunca las de otro.

    `organo_id`/`region_id` tienen los mismos límites que los ids de los
    schemas: la columna es INTEGER."""
    items, total = await servicio.listar_alertas(
        db,
        actual.id,
        offset=paginacion.offset,
        limit=paginacion.size,
        organo_id=organo_id,
        region_id=region_id,
        activa=activa,
    )
    return Pagina[AlertaRead](items=items, total=total, page=paginacion.page, size=paginacion.size)


@router.post("", response_model=AlertaRead, status_code=status.HTTP_201_CREATED, summary="Crear alerta")
async def crear_alerta(datos: AlertaCreate, db: DbDep, actual: UsuarioActualDep) -> AlertaRead:
    return await servicio.crear_alerta(db, datos, actual.id)


@router.get("/{alerta_id}", response_model=AlertaRead, responses=_RESPUESTA_404, summary="Consultar una alerta")
async def obtener_alerta(alerta_id: int, db: DbDep, actual: UsuarioActualDep) -> AlertaRead:
    try:
        return await servicio.obtener_alerta(db, alerta_id, actual.id)
    except servicio.AlertaNoEncontrada as exc:
        raise _NO_ENCONTRADA from exc


@router.patch("/{alerta_id}", response_model=AlertaRead, responses=_RESPUESTA_404, summary="Editar alerta")
async def actualizar_alerta(
    alerta_id: int, datos: AlertaUpdate, db: DbDep, actual: UsuarioActualDep
) -> AlertaRead:
    """Actualización parcial. `organos`/`regiones`, si vienen, reemplazan la
    lista entera; `[]` la vacía."""
    try:
        return await servicio.actualizar_alerta(db, alerta_id, datos, actual.id)
    except servicio.AlertaNoEncontrada as exc:
        raise _NO_ENCONTRADA from exc
    except servicio.RangoFechasInvalido as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get(
    "/{alerta_id}/ejecuciones",
    response_model=Pagina[EjecucionRead],
    responses=_RESPUESTA_404,
    summary="Historial de ejecuciones de una alerta",
)
async def listar_ejecuciones(
    alerta_id: int,
    db: DbDep,
    paginacion: PaginacionDep,
    actual: UsuarioActualDep,
    estado_envio: Annotated[
        EstadoEnvio | None, Query(description="Filtra por resultado del envío; p. ej. 'error'.")
    ] = None,
) -> Pagina[EjecucionRead]:
    """La ejecución más reciente primero. Solo el resumen de cada una: las
    convocatorias detectadas están en el detalle.

    Las escribe el motor de alertas, así que hasta que exista el historial
    está vacío."""
    try:
        items, total = await servicio_ejecuciones.listar_ejecuciones(
            db,
            alerta_id,
            actual.id,
            offset=paginacion.offset,
            limit=paginacion.size,
            estado_envio=estado_envio,
        )
    except servicio.AlertaNoEncontrada as exc:
        raise _NO_ENCONTRADA from exc
    return Pagina[EjecucionRead](items=items, total=total, page=paginacion.page, size=paginacion.size)


@router.get(
    "/{alerta_id}/ejecuciones/{ejecucion_id}",
    response_model=EjecucionDetalle,
    responses=_RESPUESTA_404,
    summary="Consultar una ejecución",
)
async def obtener_ejecucion(
    alerta_id: int, ejecucion_id: int, db: DbDep, actual: UsuarioActualDep
) -> EjecucionDetalle:
    """Añade al resumen las convocatorias detectadas, tal como quedaron en la
    caché local al registrarlas."""
    try:
        return await servicio_ejecuciones.obtener_ejecucion(db, alerta_id, ejecucion_id, actual.id)
    except servicio_ejecuciones.EjecucionNoEncontrada as exc:
        raise _NO_ENCONTRADA_EJECUCION from exc


@router.delete(
    "/{alerta_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_RESPUESTA_404, summary="Eliminar alerta"
)
async def eliminar_alerta(alerta_id: int, db: DbDep, actual: UsuarioActualDep) -> None:
    """Borrado físico: se lleva también su histórico de ejecuciones. Para
    pausar una alerta sin perderlo, `PATCH` con `activa: false`."""
    try:
        await servicio.eliminar_alerta(db, alerta_id, actual.id)
    except servicio.AlertaNoEncontrada as exc:
        raise _NO_ENCONTRADA from exc
