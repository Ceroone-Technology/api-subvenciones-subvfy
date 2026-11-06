"""Inicio page data (plan N10). Any authenticated role; everything is scoped to the caller (user and company)."""

from fastapi import APIRouter

from app.api.deps import DbDep, UsuarioActualDep
from app.schemas.inicio import InicioResumen
from app.services import inicio as servicio

router = APIRouter(prefix="/inicio", tags=["inicio"])


@router.get("/resumen", response_model=InicioResumen, summary="Contadores de la página de inicio")
async def resumen(db: DbDep, actual: UsuarioActualDep) -> InicioResumen:
    """Novedades desde tu acceso anterior, cierres en 7 días, alertas con resultados y tarjetas en seguimiento."""
    return await servicio.resumen(db, actual)
