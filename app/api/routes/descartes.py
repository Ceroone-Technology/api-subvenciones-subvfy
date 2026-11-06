"""Discards of the user (N4). Personal like favorites: any role, only the caller's own, never a colleague's."""

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import DbDep, UsuarioActualDep
from app.schemas.descarte import DescarteCreate, DescarteRead
from app.services import descartes as servicio

router = APIRouter(prefix="/descartes", tags=["descartes"])


@router.post(
    "",
    response_model=DescarteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Ocultar una convocatoria (No me interesa)",
    responses={
        200: {"description": "Ya la tenías descartada: no cambia nada."},
        404: {"description": "Convocatoria desconocida."},
    },
)
async def descartar(datos: DescarteCreate, db: DbDep, actual: UsuarioActualDep, response: Response) -> DescarteRead:
    """El radar y las novedades dejan de mostrártela; el buscador sí la sigue mostrando. Es idempotente."""
    try:
        descarte, creado = await servicio.descartar(db, actual.id, datos.codigo_bdns)
    except servicio.ConvocatoriaDesconocida:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Convocatoria no encontrada.") from None
    if not creado:
        response.status_code = status.HTTP_200_OK
    return DescarteRead(codigo_bdns=datos.codigo_bdns, descartada_at=descarte.created_at)


@router.delete(
    "/{codigo_bdns}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Volver a mostrar una convocatoria descartada",
    responses={404: {"description": "No tienes descartada esa convocatoria."}},
)
async def recuperar(codigo_bdns: str, db: DbDep, actual: UsuarioActualDep) -> Response:
    try:
        await servicio.recuperar(db, actual.id, codigo_bdns)
    except servicio.DescarteInexistente:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No tienes descartada esa convocatoria.") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
