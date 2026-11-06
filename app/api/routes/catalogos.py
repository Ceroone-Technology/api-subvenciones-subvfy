"""Catálogos locales en forma de nodo BDNS (`bdns-local-sync`, PR5).

Autenticación a nivel de router (D13). No hay `/catalogos/acciones`: esa taxonomía propia está fuera de alcance.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import DbDep
from app.core.permisos import usuario_actual
from app.models import SectorActividad
from app.schemas.catalogo import NodoCatalogo
from app.schemas.convocatoria import NivelAdministracion
from app.schemas.empresa import SectorActividadRead
from app.services import catalogos_consulta as consulta

router = APIRouter(prefix="/catalogos", tags=["catalogos"], dependencies=[Depends(usuario_actual)])


@router.get("/organos", response_model=list[NodoCatalogo], summary="Árbol de órganos")
async def organos(db: DbDep, nivel: NivelAdministracion | None = None) -> list[NodoCatalogo]:
    return await consulta.arbol(db, "organo", nivel)


@router.get("/regiones", response_model=list[NodoCatalogo], summary="Árbol de regiones")
async def regiones(db: DbDep) -> list[NodoCatalogo]:
    return await consulta.arbol(db, "region")


@router.get("/actividades", response_model=list[SectorActividadRead], summary="Actividades de empresa (CNAE)")
async def actividades(db: DbDep) -> list[SectorActividad]:
    """Las 21 secciones CNAE-2009 más `OTRA`, en el orden en que se muestran."""
    return list((await db.execute(select(SectorActividad).order_by(SectorActividad.orden))).scalars())


@router.get("/beneficiarios", response_model=list[NodoCatalogo], summary="Tipos de beneficiario")
async def beneficiarios(db: DbDep) -> list[NodoCatalogo]:
    return await consulta.beneficiarios(db)
