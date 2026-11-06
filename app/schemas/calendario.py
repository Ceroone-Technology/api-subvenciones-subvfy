"""Schemas of the calendar. Field names mirror the frontend contract (`calendario.types.ts`) in snake_case."""

from datetime import date
from typing import Literal

from pydantic import BaseModel

TipoEvento = Literal["cierre", "apertura", "tarea"]
OrigenEvento = Literal["favoritos", "seguimiento"]


class EventoCalendario(BaseModel):
    id: str
    fecha: date
    titulo: str
    tipo: TipoEvento
    origen: OrigenEvento
    codigo_bdns: str | None


class EnlaceSuscripcion(BaseModel):
    """Private URL of the calendar feed: a secret, whoever has it reads the dates."""

    url: str
