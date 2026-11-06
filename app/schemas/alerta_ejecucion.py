"""Schemas del historial de ejecuciones de una alerta.

Es una vista **de solo lectura**: las filas de `alerta_ejecucion` las escribirá
el motor de alertas, que todavía no existe. De ahí que no haya schema de
entrada.

El listado devuelve el resumen (`EjecucionRead`) y el detalle añade las
convocatorias detectadas (`EjecucionDetalle`): la pantalla de historial no
necesita cargar las fichas de todas las ejecuciones para pintar la lista.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.alerta_ejecucion import ESTADOS_ENVIO
from app.schemas.convocatoria import ConvocatoriaRead

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí el ignore.
EstadoEnvio = Literal[ESTADOS_ENVIO]  # type: ignore[valid-type]


class EjecucionRead(BaseModel):
    """Una ejecución del motor sobre una alerta, sin las convocatorias."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    alerta_id: int
    fecha_ejecucion_at: datetime
    convocatorias_encontradas: int
    estado_envio: str
    # Solo viene relleno cuando estado_envio es "error".
    detalle_error: str | None
    created_at: datetime


class EjecucionDetalle(EjecucionRead):
    """Las convocatorias salen de la caché local (`convocatoria`), que es lo
    que se guardó al detectarlas; no se vuelve a consultar la BDNS."""

    convocatorias: list[ConvocatoriaRead]
