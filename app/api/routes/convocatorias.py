"""Búsqueda de convocatorias sobre la copia local (`bdns-local-sync`, PR4).

Todo el router exige un usuario autenticado de cualquier rol (D13): la dependencia
está a nivel de router para que una ruta nueva no pueda olvidarla. Los datos son
globales, sin filtro por empresa. Nada de aquí llama a la BDNS.

**Orden de declaración**: `/resumen` va antes de `/{codigo_bdns}`, o la segunda lo capturaría.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DbDep, PaginacionDep
from app.core.permisos import usuario_actual
from app.schemas.common import Pagina
from app.schemas.convocatoria import (
    ConvocatoriaDetalle,
    ConvocatoriaResumen,
    DocumentoConvocatoria,
    NivelAdministracion,
    ResumenConvocatorias,
)
from app.services import busqueda_convocatorias as busqueda
from app.services.busqueda_convocatorias import FiltrosBusqueda, OrdenConvocatorias
from app.services.convocatoria_estado import EstadoConvocatoria

router = APIRouter(prefix="/convocatorias", tags=["convocatorias"], dependencies=[Depends(usuario_actual)])

MAX_TEXTO = 200


def filtros_busqueda(
    q: Annotated[str | None, Query(max_length=MAX_TEXTO, description="Texto libre (sin distinguir acentos).")] = None,
    nivel: NivelAdministracion | None = None,
    organo: Annotated[list[int] | None, Query(description="Ids BDNS de órgano; un padre incluye a sus hijos.")] = None,
    region: Annotated[
        list[int] | None, Query(description="Ids BDNS de región; una región incluye a sus hijas.")
    ] = None,
    beneficiario: Annotated[
        list[str] | None, Query(description="Tipos de beneficiario, tal cual los da el catálogo.")
    ] = None,
    estado: Annotated[list[EstadoConvocatoria] | None, Query()] = None,
    registro_desde: date | None = None,
    registro_hasta: date | None = None,
    plazo_desde: Annotated[date | None, Query(description="Fin de solicitud desde esta fecha.")] = None,
    plazo_hasta: Annotated[date | None, Query(description="Fin de solicitud hasta esta fecha.")] = None,
    presupuesto_min: Annotated[Decimal | None, Query(ge=0)] = None,
    presupuesto_max: Annotated[Decimal | None, Query(ge=0)] = None,
    solo_mrr: bool = False,
) -> FiltrosBusqueda:
    return FiltrosBusqueda(
        q=q, nivel=nivel, organo=organo or (), region=region or (), beneficiario=beneficiario or (),
        estado=estado or (), registro_desde=registro_desde, registro_hasta=registro_hasta,
        plazo_desde=plazo_desde, plazo_hasta=plazo_hasta, presupuesto_min=presupuesto_min,
        presupuesto_max=presupuesto_max, solo_mrr=solo_mrr,
    )  # fmt: skip


FiltrosDep = Annotated[FiltrosBusqueda, Depends(filtros_busqueda)]


@router.get("", response_model=Pagina[ConvocatoriaResumen], summary="Buscar convocatorias")
async def listar(
    db: DbDep,
    filtros: FiltrosDep,
    paginacion: PaginacionDep,
    orden: Annotated[OrdenConvocatorias, Query(description="`relevancia` solo ordena si hay `q`.")] = "recientes",
) -> Pagina[ConvocatoriaResumen]:
    total, filas = await busqueda.buscar(db, filtros, orden, offset=paginacion.offset, limit=paginacion.size)
    return Pagina(
        items=[ConvocatoriaResumen.model_validate(f) for f in filas],
        total=total,
        page=paginacion.page,
        size=paginacion.size,
    )


@router.get("/resumen", response_model=ResumenConvocatorias, summary="Cifras de cabecera del buscador")
async def resumen(db: DbDep) -> ResumenConvocatorias:
    return ResumenConvocatorias.model_validate(await busqueda.resumen(db))


@router.get("/{codigo_bdns}", response_model=ConvocatoriaDetalle, summary="Ficha de una convocatoria")
async def detalle(codigo_bdns: str, db: DbDep) -> ConvocatoriaDetalle:
    encontrada = await busqueda.obtener(db, codigo_bdns)
    if encontrada is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Convocatoria no encontrada.")
    conv, estado = encontrada
    ficha = conv.detalle or {}
    resumen_fila = {col.name: getattr(conv, col.name) for col in busqueda.COLUMNAS_LISTADO}
    return ConvocatoriaDetalle(
        **resumen_fila,
        estado=estado,
        sede_electronica=conv.sede_electronica,
        url_bases_reguladoras=conv.url_bases_reguladoras,
        beneficiarios=conv.beneficiarios,
        sectores=conv.sectores,
        instrumentos=ficha.get("instrumentos", []),
        fondos=ficha.get("fondos", []),
        objetivos=ficha.get("objetivos", []),
        regiones=ficha.get("regiones", []),
        bases_reguladoras=ficha.get("bases_reguladoras"),
        documentos=[
            DocumentoConvocatoria(**doc, url=f"/convocatorias/{conv.codigo_bdns}/documentos/{doc['id']}")
            for doc in ficha.get("documentos", [])
        ],
        retirada=conv.removed_at is not None,
        ultima_actualizacion=conv.detalle_sync_at,
    )
