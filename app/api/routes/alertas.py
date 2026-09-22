"""Alertas del usuario (Hito 4, Funcionalidad 1): mutaciones.

El router solo valida la entrada (schemas) y traduce a HTTP los errores del
servicio; la lógica está en `app.services.alertas`. Los GET de listado y
detalle llegan en la siguiente tarea del backlog.
"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import DbDep, UsuarioActualDep
from app.schemas.alerta import AlertaCreate, AlertaRead, AlertaUpdate
from app.services import alertas as servicio

router = APIRouter(prefix="/alertas", tags=["alertas"])

_NO_ENCONTRADA = HTTPException(status.HTTP_404_NOT_FOUND, detail="Alerta no encontrada.")


@router.post("", response_model=AlertaRead, status_code=status.HTTP_201_CREATED, summary="Crear alerta")
async def crear_alerta(datos: AlertaCreate, db: DbDep, actual: UsuarioActualDep) -> AlertaRead:
    return await servicio.crear_alerta(db, datos, actual.id)


@router.patch("/{alerta_id}", response_model=AlertaRead, summary="Editar alerta")
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


@router.delete("/{alerta_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar alerta")
async def eliminar_alerta(alerta_id: int, db: DbDep, actual: UsuarioActualDep) -> None:
    """Borrado físico: se lleva también su histórico de ejecuciones. Para
    pausar una alerta sin perderlo, `PATCH` con `activa: false`."""
    try:
        await servicio.eliminar_alerta(db, alerta_id, actual.id)
    except servicio.AlertaNoEncontrada as exc:
        raise _NO_ENCONTRADA from exc
