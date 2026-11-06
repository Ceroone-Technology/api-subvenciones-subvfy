"""Catálogos locales en forma de nodo BDNS (`bdns-local-sync`, PR5).

Autenticación a nivel de router (D13). No hay `/catalogos/acciones`: esa taxonomía propia está fuera de alcance.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import DbDep
from app.core.permisos import usuario_actual
from app.models import Interes, SectorActividad
from app.schemas.catalogo import NodoCatalogo
from app.schemas.convocatoria import NivelAdministracion, TemaRead
from app.schemas.empresa import InteresRead, SectorActividadRead
from app.services import catalogos_consulta as consulta
from app.services.clasificacion import CATALOGO_TEMAS

router = APIRouter(prefix="/catalogos", tags=["catalogos"], dependencies=[Depends(usuario_actual)])


@router.get("/organos", response_model=list[NodoCatalogo], summary="Árbol de órganos")
async def organos(db: DbDep, nivel: NivelAdministracion | None = None) -> list[NodoCatalogo]:
    return await consulta.arbol(db, "organo", nivel)


@router.get("/regiones", response_model=list[NodoCatalogo], summary="Árbol de regiones")
async def regiones(db: DbDep) -> list[NodoCatalogo]:
    return await consulta.arbol(db, "region")


@router.get("/actividades", response_model=list[SectorActividadRead], summary="Actividades de empresa (CNAE)")
async def actividades(db: DbDep) -> list[SectorActividad]:
    """Las 22 secciones CNAE-2025 (las de la BDNS) más `OTRA`, en el orden en que se muestran."""
    return list((await db.execute(select(SectorActividad).order_by(SectorActividad.orden))).scalars())


@router.get("/intereses", response_model=list[InteresRead], summary="Intereses de inversión de la empresa")
async def intereses(db: DbDep) -> list[Interes]:
    """Los 8 intereses que una empresa puede marcar (`PATCH /empresas/{id}`, campo `intereses`), en orden."""
    return list((await db.execute(select(Interes).order_by(Interes.orden))).scalars())


@router.get("/temas", response_model=list[TemaRead], summary="Temas de convocatoria (clasificación propia)")
async def temas() -> list[TemaRead]:
    """Los 6 temas en el orden en que se muestran. Fijos: no dependen de la base de datos."""
    return [TemaRead(**asdict(t)) for t in CATALOGO_TEMAS]


@router.get("/beneficiarios", response_model=list[NodoCatalogo], summary="Tipos de beneficiario")
async def beneficiarios(db: DbDep) -> list[NodoCatalogo]:
    return await consulta.beneficiarios(db)
