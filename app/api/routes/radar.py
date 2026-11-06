"""Radar of opportunities for the company of the session (N3). Any role; nothing here calls the BDNS."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import DbDep, UsuarioActualDep
from app.core.permisos import usuario_actual
from app.models.convocatoria import TEMAS
from app.schemas.convocatoria import TemaConvocatoria
from app.schemas.radar import OportunidadRadar, ResultadoRadar
from app.services import radar as servicio

router = APIRouter(prefix="/radar", tags=["radar"], dependencies=[Depends(usuario_actual)])


@router.get("", response_model=ResultadoRadar, summary="Convocatorias abiertas ordenadas por afinidad con tu empresa")
async def obtener(
    db: DbDep,
    actual: UsuarioActualDep,
    min: Annotated[int, Query(ge=0, le=100, description="Afinidad mínima (0-100).")] = 0,
    tema: Annotated[
        list[TemaConvocatoria] | None,
        Query(max_length=len(TEMAS), description="Temas propios (`/catalogos/temas`); varios se suman (OR)."),
    ] = None,
    gestiona: Annotated[
        bool | None, Query(description="`true`: solo las que gestiona Subvfy; `false`: solo las demás.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Máximo de oportunidades devueltas.")] = 50,
) -> ResultadoRadar:
    """Afinidad determinista (CNAE 40, zona 20, tamaño 15, intereses 15, plazo 10), calculada al vuelo.

    Solo convocatorias abiertas; si la empresa tiene actividad CNAE, se parte de las de su sector o sin sector.
    Cada oportunidad explica su puntuación con `motivos` (por qué encaja) y `dudas` (qué comprobar).
    """
    total, oportunidades = await servicio.radar(
        db, actual.empresa_id, minimo=min, temas=tema or (), gestiona=gestiona, limite=limit
    )
    items = [
        OportunidadRadar(
            codigo_bdns=o.fila["codigo_bdns"],
            titulo=o.fila["titulo"],
            organo=o.fila["organo_convocante"] or o.fila["administracion"],
            tema=o.fila["tema"],
            gestiona_subvfy=o.fila["gestiona_subvfy"],
            afinidad=o.resultado.afinidad,
            motivos=o.resultado.motivos,
            dudas=o.resultado.dudas,
            dias_restantes=None if o.dias_restantes is None else max(0, o.dias_restantes),
            presupuesto_total=None if o.fila["presupuesto_total"] is None else float(o.fila["presupuesto_total"]),
            zona=o.resultado.zona,
        )
        for o in oportunidades
    ]
    return ResultadoRadar(items=items, total=total)
