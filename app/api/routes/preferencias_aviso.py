"""Notice preferences of the signed-in user (plan N9a). Personal: there is no way to read or write another user's."""

from typing import Annotated

from fastapi import APIRouter, Body

from app.api.deps import DbDep, UsuarioActualDep
from app.models.preferencia_aviso import TIPOS_AVISO
from app.schemas.preferencia_aviso import PreferenciaAvisoItem
from app.services import preferencias_aviso as servicio

router = APIRouter(prefix="/me", tags=["cuenta"])


@router.get(
    "/preferencias-aviso",
    response_model=list[PreferenciaAvisoItem],
    summary="Mis preferencias de aviso (siempre los cinco tipos)",
)
async def obtener_preferencias(db: DbDep, actual: UsuarioActualDep) -> list[PreferenciaAvisoItem]:
    return await servicio.obtener(db, actual.id)


@router.put(
    "/preferencias-aviso",
    response_model=list[PreferenciaAvisoItem],
    summary="Guardar mis preferencias de aviso",
)
async def guardar_preferencias(
    preferencias: Annotated[list[PreferenciaAvisoItem], Body(min_length=1, max_length=len(TIPOS_AVISO))],
    db: DbDep,
    actual: UsuarioActualDep,
) -> list[PreferenciaAvisoItem]:
    """Reemplaza los tipos enviados y deja como estaban los demás. Devuelve siempre los cinco."""
    return await servicio.guardar(db, actual.id, preferencias)
