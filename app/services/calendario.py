"""Calendar of deadlines (plan N6): the user's favorites plus the company's follow-up board.

Sources: `favoritos` (personal, like the favorites themselves) and `seguimiento` (the company board, plus its tasks
with a due date). A grant that is in both appears once per origin, so the frontend filter by origin keeps working.
The feed is addressed by a secret token (`usuario.calendario_token`): whoever has the URL reads the dates, so the
token is 256 bits, shown only to its owner and replaced on rotation.
"""

import secrets
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.permisos import UsuarioAutenticado, motivo_cuenta_no_operativa
from app.models import Convocatoria, Empresa, Favorito, Seguimiento, SeguimientoTarea, Usuario
from app.schemas.calendario import EventoCalendario
from app.services.ics import EventoIcs, construir_ics

ORIGENES = ("favoritos", "seguimiento")
MAX_DIAS_RANGO = 400
# Window of the downloaded file and of the subscription feed.
DIAS_ATRAS_FEED = 30
DIAS_ADELANTE_FEED = 365
TOKEN_BYTES = 32


def _hoy() -> date:
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def parsear_origenes(valores: list[str] | None) -> tuple[str, ...]:
    """`origen=favoritos,seguimiento` (or the parameter repeated). Empty = both; anything else is a 422."""
    pedidos = [p.strip() for v in valores or [] for p in v.split(",") if p.strip()]
    desconocidos = sorted(set(pedidos) - set(ORIGENES))
    if desconocidos:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"origen desconocido: {', '.join(desconocidos)}. Valores: {', '.join(ORIGENES)}.",
        )
    return tuple(o for o in ORIGENES if o in pedidos) or ORIGENES


def validar_rango(desde: date, hasta: date) -> None:
    if hasta < desde:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="hasta no puede ser anterior a desde.")
    if (hasta - desde).days > MAX_DIAS_RANGO:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"El rango no puede superar {MAX_DIAS_RANGO} días."
        )


def _fechas_de_plazo(origen: str, id_fila: int, c: Convocatoria, desde: date, hasta: date) -> list[EventoCalendario]:
    prefijo = "fav" if origen == "favoritos" else "seg"
    eventos: list[EventoCalendario] = []
    for tipo, fecha in (("cierre", c.fecha_fin_solicitud), ("apertura", c.fecha_inicio_solicitud)):
        if fecha is not None and desde <= fecha <= hasta:
            eventos.append(
                EventoCalendario(
                    id=f"{prefijo}-{tipo}-{id_fila}",
                    fecha=fecha,
                    titulo=c.titulo,
                    tipo=tipo,
                    origen=origen,
                    codigo_bdns=c.codigo_bdns,
                )
            )
    return eventos


async def eventos(
    db: AsyncSession, actual: UsuarioAutenticado | Usuario, desde: date, hasta: date, origenes: tuple[str, ...]
) -> list[EventoCalendario]:
    usuario_id, empresa_id = actual.id, actual.empresa_id
    resultado: list[EventoCalendario] = []
    en_rango = (
        (Convocatoria.fecha_fin_solicitud.between(desde, hasta))
        | (Convocatoria.fecha_inicio_solicitud.between(desde, hasta))
    )  # fmt: skip
    if "favoritos" in origenes:
        filas = await db.execute(
            select(Favorito.id, Convocatoria)
            .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
            .where(Favorito.usuario_id == usuario_id, en_rango)
        )
        for id_fila, c in filas.all():
            resultado += _fechas_de_plazo("favoritos", id_fila, c, desde, hasta)
    if "seguimiento" in origenes:
        filas = await db.execute(
            select(Seguimiento.id, Convocatoria)
            .join(Convocatoria, Convocatoria.id == Seguimiento.convocatoria_id)
            .where(Seguimiento.empresa_id == empresa_id, en_rango)
        )
        for id_fila, c in filas.all():
            resultado += _fechas_de_plazo("seguimiento", id_fila, c, desde, hasta)
        tareas = await db.execute(
            select(SeguimientoTarea, Convocatoria.codigo_bdns)
            .join(Seguimiento, Seguimiento.id == SeguimientoTarea.seguimiento_id)
            .join(Convocatoria, Convocatoria.id == Seguimiento.convocatoria_id)
            .where(Seguimiento.empresa_id == empresa_id, SeguimientoTarea.fecha_limite.between(desde, hasta))
        )
        for t, codigo in tareas.all():
            assert t.fecha_limite is not None
            resultado.append(
                EventoCalendario(
                    id=f"seg-tarea-{t.id}",
                    fecha=t.fecha_limite,
                    titulo=t.titulo,
                    tipo="tarea",
                    origen="seguimiento",
                    codigo_bdns=codigo,
                )
            )
    return sorted(resultado, key=lambda e: (e.fecha, e.id))


def _a_ics(e: EventoCalendario) -> EventoIcs:
    url = None
    if e.codigo_bdns:
        ruta = settings.frontend_ruta_convocatoria.format(codigo_bdns=quote(e.codigo_bdns, safe=""))
        url = f"{settings.frontend_base_url.rstrip('/')}{ruta}"
    return EventoIcs(uid=e.id, fecha=e.fecha, titulo=e.titulo, tipo=e.tipo, url=url)


async def ics_de(db: AsyncSession, actual: UsuarioAutenticado | Usuario, origenes: tuple[str, ...]) -> str:
    hoy = _hoy()
    lista = await eventos(
        db, actual, hoy - timedelta(days=DIAS_ATRAS_FEED), hoy + timedelta(days=DIAS_ADELANTE_FEED), origenes
    )
    return construir_ics(_a_ics(e) for e in lista)


# --- Subscription token -------------------------------------------------------------------------------------


def url_de_suscripcion(token: str) -> str:
    return f"{settings.api_publica_url.rstrip('/')}/calendario/feed/{token}.ics"


async def token_de(db: AsyncSession, actual: UsuarioAutenticado) -> str:
    """The token of the user, created on first use."""
    token = await db.scalar(select(Usuario.calendario_token).where(Usuario.id == actual.id))
    if token is not None:
        return token
    return await rotar_token(db, actual)


async def rotar_token(db: AsyncSession, actual: UsuarioAutenticado) -> str:
    """Issues a new token; the previous URL stops working at once."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    await db.execute(
        update(Usuario)
        .where(Usuario.id == actual.id)
        .values(calendario_token=token, updated_by=actual.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return token


async def usuario_del_token(db: AsyncSession, token: str) -> Usuario | None:
    """The owner of a feed token, or None when it is unknown or the account cannot operate (company or user off)."""
    if not (32 <= len(token) <= 64):
        return None
    fila = (
        await db.execute(
            select(Usuario, Empresa.estado)
            .join(Empresa, Empresa.id == Usuario.empresa_id)
            .where(Usuario.calendario_token == token)
        )
    ).first()
    if fila is None or motivo_cuenta_no_operativa(fila[0].estado, fila[1]) is not None:
        return None
    return fila[0]
