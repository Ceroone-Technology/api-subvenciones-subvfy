"""Schemas of the follow-up board. Field names mirror the frontend contract (`seguimiento.types.ts`) in snake_case."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.convocatoria import TEMAS
from app.models.seguimiento import ESTADOS_SEGUIMIENTO, RESULTADOS_SEGUIMIENTO, TIPOS_EVENTO, TIPOS_TAREA
from app.schemas.convocatoria import CodigoBdns

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí los ignore.
EstadoSeguimiento = Literal[ESTADOS_SEGUIMIENTO]  # type: ignore[valid-type]
ResultadoSeguimiento = Literal[RESULTADOS_SEGUIMIENTO]  # type: ignore[valid-type]
TipoTarea = Literal[TIPOS_TAREA]  # type: ignore[valid-type]
TipoEvento = Literal[TIPOS_EVENTO]  # type: ignore[valid-type]
TemaSeguimiento = Literal[TEMAS]  # type: ignore[valid-type]


class TareaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    titulo: str
    tipo: TipoTarea
    fecha_limite: date | None
    hecha: bool
    asignado_a: int | None


class SeguimientoRead(BaseModel):
    id: int
    codigo_bdns: str
    titulo: str
    organo: str | None
    tema: TemaSeguimiento | None
    # Budget of the call, never a per-project cap.
    importe: float | None
    fecha_fin_solicitud: date | None
    estado: EstadoSeguimiento
    resultado: ResultadoSeguimiento | None
    responsable_id: int | None
    fecha_presentacion: date | None
    tareas: list[TareaRead]
    tareas_hechas: int
    tareas_total: int
    created_at: datetime
    updated_at: datetime


class SeguimientoCreate(BaseModel):
    """The call comes from the local cache; `titulo` and the optional fields only seed a call that is not there yet."""

    codigo_bdns: CodigoBdns
    titulo: str = Field(min_length=1, max_length=500)
    organo: str | None = Field(default=None, max_length=300)
    importe: float | None = Field(default=None, ge=0, lt=10**14)
    fecha_fin_solicitud: date | None = None
    tema: TemaSeguimiento | None = None


class SeguimientoUpdate(BaseModel):
    """PATCH: only what is present is applied. `resultado` and `responsable_id` accept an explicit null."""

    estado: EstadoSeguimiento | None = None
    resultado: ResultadoSeguimiento | None = None
    responsable_id: int | None = None
    fecha_presentacion: date | None = None


class TareaCreate(BaseModel):
    titulo: str = Field(min_length=1, max_length=200, pattern=r"\S")
    tipo: TipoTarea = "tarea"
    fecha_limite: date | None = None
    asignado_a: int | None = None


class TareaUpdate(BaseModel):
    titulo: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    hecha: bool | None = None
    fecha_limite: date | None = None
    asignado_a: int | None = None


class ResumenEstado(BaseModel):
    estado: EstadoSeguimiento
    total: int
    importe: float


class ResumenConcedidas(BaseModel):
    total: int
    importe: float


class SeguimientoResumen(BaseModel):
    """Funnel of the board: one entry per column (always the four, in order) plus what was granted."""

    estados: list[ResumenEstado]
    concedidas: ResumenConcedidas
    total: int
    importe_total: float


class EventoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tipo: TipoEvento
    usuario_id: int | None
    estado_anterior: str | None
    estado_nuevo: str | None
    ocurrido_at: datetime
