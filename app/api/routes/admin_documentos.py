"""Métricas de descarga de documentos (solo admin). Solo ids y recuentos, sin datos personales."""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import AdminDep, DbDep, PaginacionDep
from app.schemas.common import Pagina
from app.services import documentos

router = APIRouter(prefix="/admin/documentos", tags=["admin-documentos"])


class MetricaDocumento(BaseModel):
    convocatoria_codigo: str
    bdns_documento_id: int
    nombre: str | None
    accesos: int
    usuarios_distintos: int
    ultimo_acceso: datetime


@router.get("/metricas", response_model=Pagina[MetricaDocumento], summary="Accesos por documento")
async def metricas(
    db: DbDep,
    _: AdminDep,
    paginacion: PaginacionDep,
    desde: Annotated[date | None, Query(description="Primer día (inclusive).")] = None,
    hasta: Annotated[date | None, Query(description="Último día (inclusive).")] = None,
) -> Pagina[MetricaDocumento]:
    total, filas = await documentos.metricas(
        db, desde=desde, hasta=hasta, offset=paginacion.offset, limit=paginacion.size
    )
    return Pagina(
        items=[MetricaDocumento(**f) for f in filas], total=total, page=paginacion.page, size=paginacion.size
    )
