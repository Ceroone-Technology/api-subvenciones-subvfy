"""Búsqueda de convocatorias sobre la copia local (`bdns-local-sync`, PR4).

Todo el router exige un usuario autenticado de cualquier rol (D13): la dependencia
está a nivel de router para que una ruta nueva no pueda olvidarla. Los datos son
globales, sin filtro por empresa. Nada de aquí llama a la BDNS.

**Orden de declaración**: `/resumen`, `/mapa`, `/estadisticas` y `/export` van antes de `/{codigo_bdns}`,
o el detalle las capturaría.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import Field, StringConstraints
from sqlalchemy import select

from app.api.deps import DbDep, PaginacionDep, UsuarioActualDep
from app.core.permisos import ROL_ADMIN, ROL_GESTOR, exigir_roles, usuario_actual
from app.models import Empresa
from app.models.convocatoria import TEMAS
from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA
from app.schemas.common import Pagina
from app.schemas.convocatoria import (
    ConvocatoriaDetalle,
    ConvocatoriaResumen,
    DocumentoConvocatoria,
    MapaConvocatorias,
    NivelAdministracion,
    ResumenConvocatorias,
    TemaConvocatoria,
)
from app.schemas.empresa import CodigoActividad
from app.schemas.estadisticas import PERIODOS, EstadisticasPanel
from app.services import busqueda_convocatorias as busqueda
from app.services import documentos as servicio_documentos
from app.services import estadisticas_convocatorias as servicio_estadisticas
from app.services import exportacion_convocatorias as exportacion
from app.services import mapa_convocatorias as servicio_mapa
from app.services import radar as servicio_radar
from app.services.almacen_documentos import AlmacenDocumentos, DocumentoDemasiadoGrande
from app.services.bdns_sync_cliente import ClienteBdnsSync
from app.services.busqueda_convocatorias import FiltrosBusqueda, OrdenConvocatorias
from app.services.convocatoria_estado import EstadoConvocatoria
from app.services.exportacion_convocatorias import FormatoExportacion

router = APIRouter(prefix="/convocatorias", tags=["convocatorias"], dependencies=[Depends(usuario_actual)])

MAX_TEXTO = 200
MAX_LISTA = 50  # elementos por filtro de lista
IdBdns = Annotated[int, Field(ge=0, le=2**31 - 1)]  # int32: fuera de rango Postgres daría 500
CABECERA_FILTRO_IGNORADO = "X-Filtro-Actividad"
# The listing also sorts by affinity (needs the company of the session); the export keeps the SQL-only orders.
ORDENES_LISTADO = (*busqueda.ORDENES, "afinidad")
OrdenListado = Literal[ORDENES_LISTADO]  # type: ignore[valid-type]


async def filtros_busqueda(
    db: DbDep,
    actual: UsuarioActualDep,
    response: Response,
    q: Annotated[str | None, Query(max_length=MAX_TEXTO, description="Texto libre (sin distinguir acentos).")] = None,
    nivel: NivelAdministracion | None = None,
    organo: Annotated[
        list[IdBdns] | None,
        Query(max_length=MAX_LISTA, description="Ids BDNS de órgano; un padre incluye a sus hijos."),
    ] = None,
    region: Annotated[
        list[IdBdns] | None,
        Query(max_length=MAX_LISTA, description="Ids BDNS de región; una región incluye a sus hijas."),
    ] = None,
    beneficiario: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_TEXTO)]] | None,
        Query(max_length=MAX_LISTA, description="Tipos de beneficiario, tal cual los da el catálogo."),
    ] = None,
    estado: Annotated[list[EstadoConvocatoria] | None, Query()] = None,
    registro_desde: date | None = None,
    registro_hasta: date | None = None,
    plazo_desde: Annotated[date | None, Query(description="Fin de solicitud desde esta fecha.")] = None,
    plazo_hasta: Annotated[date | None, Query(description="Fin de solicitud hasta esta fecha.")] = None,
    presupuesto_min: Annotated[Decimal | None, Query(ge=0)] = None,
    presupuesto_max: Annotated[Decimal | None, Query(ge=0)] = None,
    solo_mrr: bool = False,
    actividad: Annotated[
        CodigoActividad | None,
        Query(description="Sección CNAE: incluye las convocatorias de esa sección y las sin sector. `OTRA` se ignora."),
    ] = None,
    para_mi_empresa: Annotated[
        bool, Query(description="Usa la actividad de tu empresa. Pendiente u `OTRA`: se ignora. `actividad` prevalece.")
    ] = False,
    tema: Annotated[
        list[TemaConvocatoria] | None,
        Query(max_length=len(TEMAS), description="Temas propios (`/catalogos/temas`); varios se suman (OR)."),
    ] = None,
    gestiona: Annotated[
        bool | None, Query(description="`true`: solo las que gestiona Subvfy; `false`: solo las demás.")
    ] = None,
) -> FiltrosBusqueda:
    seccion = actividad
    if seccion is None and para_mi_empresa:
        seccion = await db.scalar(select(Empresa.actividad_codigo).where(Empresa.id == actual.usuario.empresa_id))
        if seccion is None:  # empresa con la actividad pendiente
            response.headers[CABECERA_FILTRO_IGNORADO] = "ignorado"
    if seccion == CODIGO_ACTIVIDAD_OTRA:
        # OTRA no tiene sección CNAE que casar: se busca sin filtrar y se avisa en una cabecera.
        response.headers[CABECERA_FILTRO_IGNORADO] = "ignorado"
        seccion = None
    return FiltrosBusqueda(
        q=q, nivel=nivel, organo=organo or (), region=region or (), beneficiario=beneficiario or (),
        estado=estado or (), registro_desde=registro_desde, registro_hasta=registro_hasta,
        plazo_desde=plazo_desde, plazo_hasta=plazo_hasta, presupuesto_min=presupuesto_min,
        presupuesto_max=presupuesto_max, solo_mrr=solo_mrr, actividad=(seccion,) if seccion else (),
        tema=tema or (), gestiona=gestiona,
    )  # fmt: skip


FiltrosDep = Annotated[FiltrosBusqueda, Depends(filtros_busqueda)]


@router.get("", response_model=Pagina[ConvocatoriaResumen], summary="Buscar convocatorias")
async def listar(
    db: DbDep,
    actual: UsuarioActualDep,
    filtros: FiltrosDep,
    paginacion: PaginacionDep,
    orden: Annotated[
        OrdenListado,
        Query(
            description="`relevancia` solo ordena si hay `q`. `cierre`: plazo más próximo primero. "
            "`afinidad`: mayor afinidad con tu empresa primero (cada fila trae su `afinidad`)."
        ),
    ] = "recientes",
) -> Pagina[ConvocatoriaResumen]:
    if orden == "afinidad":
        total, filas = await servicio_radar.buscar_por_afinidad(
            db, filtros, actual.empresa_id, offset=paginacion.offset, limit=paginacion.size
        )
    else:
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


@router.get(
    "/mapa",
    response_model=MapaConvocatorias,
    summary="Totales por comunidad para el mapa",
    description="Mismos filtros que el listado (`page`, `size` y `orden` no se usan). Una convocatoria en varias "
    "comunidades cuenta en cada una: la suma de comunidades puede superar `total`. El presupuesto es el total de "
    "la convocatoria, sin repartir. Caché en memoria de 60 s.",
)
async def mapa(db: DbDep, filtros: FiltrosDep) -> MapaConvocatorias:
    return MapaConvocatorias.model_validate(await servicio_mapa.mapa(db, filtros))


@router.get(
    "/estadisticas",
    response_model=EstadisticasPanel,
    summary="Estadísticas de convocatorias para el panel",
    dependencies=[Depends(exigir_roles(ROL_ADMIN, ROL_GESTOR))],
    description="Solo admin y gestor. Cifras globales: KPIs con variación del periodo (`dias` 7, 30 o 90), "
    "publicadas por semana (12 semanas), fondos por tema y cierres de los próximos 30 días. "
    "`embudo` va vacío hasta que exista el CRM. Caché en memoria de 60 s.",
)
async def estadisticas(db: DbDep, dias: Annotated[int, Query(description="7, 30 o 90.")] = 30) -> EstadisticasPanel:
    if dias not in PERIODOS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="`dias` debe ser 7, 30 o 90.")
    return await servicio_estadisticas.estadisticas(db, dias)


@router.get("/export", summary="Exportar el resultado filtrado a CSV o XLSX")
async def exportar(
    db: DbDep,
    filtros: FiltrosDep,
    formato: FormatoExportacion,
    orden: Annotated[OrdenConvocatorias, Query()] = "recientes",
) -> Response:
    """Mismos filtros que el listado, tope de 5.000 filas, sin llamar a la BDNS."""
    datos = await exportacion.filas(db, filtros, orden)
    if formato == "csv":
        contenido, tipo = exportacion.a_csv(datos), exportacion.MIME_CSV
    else:
        contenido, tipo = exportacion.a_xlsx(datos), exportacion.MIME_XLSX
    nombre = f"convocatorias-{datetime.now(UTC):%Y%m%d}.{formato}"
    return Response(contenido, media_type=tipo, headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


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
        # `url` es siempre la ruta propia (nunca un host de la BDNS).
        documentos=[
            DocumentoConvocatoria(**doc, url=f"/convocatorias/{codigo_bdns}/documentos/{doc['id']}")
            for doc in ficha.get("documentos", [])
        ],
        retirada=conv.removed_at is not None,
        ultima_actualizacion=conv.detalle_sync_at,
    )


ClienteDocumentosDep = Annotated[ClienteBdnsSync, Depends(servicio_documentos.cliente_bdns)]
AlmacenDep = Annotated[AlmacenDocumentos, Depends(servicio_documentos.almacen)]


@router.get(
    "/{codigo_bdns}/documentos/{id_documento}",
    summary="Descargar un documento de la convocatoria",
    response_class=StreamingResponse,
    responses={
        404: {"description": "Convocatoria o documento desconocidos."},
        413: {"description": "El documento supera el máximo permitido (`documento_demasiado_grande`)."},
        503: {"description": "La BDNS no está disponible y el documento aún no está en nuestro almacén."},
    },
)
async def descargar_documento(
    codigo_bdns: str,
    id_documento: IdBdns,
    db: DbDep,
    actual: UsuarioActualDep,
    almacen: AlmacenDep,
    cliente: ClienteDocumentosDep,
) -> Response:
    """La primera vez se baja de la BDNS (con límite de ritmo) y se guarda; después sale de nuestro almacén."""
    try:
        listo = await servicio_documentos.servir(
            db,
            usuario_id=actual.id,
            codigo_bdns=codigo_bdns,
            id_documento=id_documento,
            almacen_documentos=almacen,
            cliente=cliente,
        )
    except servicio_documentos.DocumentoNoEncontrado:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.") from None
    except DocumentoDemasiadoGrande:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "codigo": "documento_demasiado_grande",
                "mensaje": "El documento supera el tamaño máximo permitido.",
            },
        ) from None
    except servicio_documentos.BdnsDocumentoNoDisponible:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo obtener el documento ahora. Inténtalo de nuevo más tarde.",
        ) from None
    disposicion = _content_disposition(listo.nombre)
    temporal = almacen.url_temporal(listo.clave, mime=listo.mime, content_disposition=disposicion)
    if temporal is not None:
        return RedirectResponse(
            temporal, status_code=status.HTTP_302_FOUND, headers={"Cache-Control": "private, no-store"}
        )
    return StreamingResponse(
        almacen.abrir(listo.clave),
        media_type=listo.mime,
        headers={
            "Content-Disposition": disposicion,
            "Content-Length": str(listo.tamano),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


def _content_disposition(nombre: str) -> str:
    ascii_ = nombre.encode("ascii", "replace").decode().replace("?", "_")
    return f"attachment; filename=\"{ascii_}\"; filename*=UTF-8''{quote(nombre)}"
