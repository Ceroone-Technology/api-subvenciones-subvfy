"""Calendar of deadlines and its ICS subscription (plan N6). Rules and sources in `app.services.calendario`."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import DbDep, UsuarioActualDep
from app.schemas.calendario import EnlaceSuscripcion, EventoCalendario
from app.services import calendario as servicio

router = APIRouter(prefix="/calendario", tags=["calendario"])

MEDIA_ICS = "text/calendar; charset=utf-8"
OrigenQuery = Annotated[
    list[str] | None, Query(description="`favoritos`, `seguimiento` o ambos separados por coma. Vacío = ambos.")
]


@router.get("", response_model=list[EventoCalendario], summary="Eventos del calendario en un rango")
async def listar_eventos(
    db: DbDep,
    actual: UsuarioActualDep,
    desde: Annotated[date, Query(description="Inclusive.")],
    hasta: Annotated[date, Query(description="Inclusive. Máximo 400 días desde `desde`.")],
    origen: OrigenQuery = None,
) -> list[EventoCalendario]:
    servicio.validar_rango(desde, hasta)
    return await servicio.eventos(db, actual, desde, hasta, servicio.parsear_origenes(origen))


@router.get(
    "/export.ics",
    summary="Descargar el calendario como .ics",
    response_class=Response,
    responses={200: {"content": {"text/calendar": {}}, "description": "Archivo iCalendar."}},
)
async def exportar_ics(db: DbDep, actual: UsuarioActualDep, origen: OrigenQuery = None) -> Response:
    contenido = await servicio.ics_de(db, actual, servicio.parsear_origenes(origen))
    return Response(
        contenido,
        media_type=MEDIA_ICS,
        headers={"Content-Disposition": 'attachment; filename="subvfy-calendario.ics"', "Cache-Control": "no-store"},
    )


@router.get("/ics-token", response_model=EnlaceSuscripcion, summary="URL privada de suscripción al calendario")
async def enlace(db: DbDep, actual: UsuarioActualDep) -> EnlaceSuscripcion:
    return EnlaceSuscripcion(url=servicio.url_de_suscripcion(await servicio.token_de(db, actual)))


@router.post(
    "/ics-token/rotar", response_model=EnlaceSuscripcion, summary="Generar una URL nueva (la anterior deja de valer)"
)
async def rotar(db: DbDep, actual: UsuarioActualDep) -> EnlaceSuscripcion:
    return EnlaceSuscripcion(url=servicio.url_de_suscripcion(await servicio.rotar_token(db, actual)))


@router.get(
    "/feed/{fichero}",
    summary="Feed ICS público, autenticado por el token de la URL",
    response_class=Response,
    responses={
        200: {"content": {"text/calendar": {}}, "description": "Archivo iCalendar."},
        404: {"description": "Token desconocido o cuenta no operativa."},
    },
)
async def feed(fichero: str, db: DbDep) -> Response:
    """Sin cabecera de autenticación: los clientes de calendario no la envían. El secreto es el token. Un token
    desconocido, mal formado o de una cuenta no operativa da el mismo 404."""
    token, punto, extension = fichero.rpartition(".")
    usuario = await servicio.usuario_del_token(db, token) if punto and extension == "ics" else None
    if usuario is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Calendario no encontrado.")
    contenido = await servicio.ics_de(db, usuario, servicio.ORIGENES)
    return Response(
        contenido,
        media_type=MEDIA_ICS,
        headers={
            "Cache-Control": "private, max-age=300",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
