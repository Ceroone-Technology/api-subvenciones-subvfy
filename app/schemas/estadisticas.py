from typing import Literal

from pydantic import BaseModel

PeriodoPanel = Literal[7, 30, 90]
PERIODOS = (7, 30, 90)


class KpiPanel(BaseModel):
    clave: str
    etiqueta: str
    valor: str  # formatted for display, es-ES ("3.059", "2.418 M€")
    variacion: str  # signed, formatted ("+124", "+186 M€")
    pie: str
    valor_numero: float
    variacion_numero: float


class PuntoSemana(BaseModel):
    etiqueta: str  # Monday of the week, "18 ago"
    todas: int
    tecnologicas: int  # those Subvfy manages (`gestiona_subvfy`)


class FondoTema(BaseModel):
    tema: str
    millones: int
    gestiona: bool


class CierreDia(BaseModel):
    etiqueta: str  # "Hoy", "Día +1" ...
    cierres: int
    proximo: bool  # closes within 7 days


class EtapaEmbudo(BaseModel):
    etapa: str
    total: int


class EstadisticasPanel(BaseModel):
    """`GET /convocatorias/estadisticas?dias=7|30|90`. Same shape as the frontend `EstadisticasPanel`."""

    periodo: PeriodoPanel
    kpis: list[KpiPanel]
    semanas: list[PuntoSemana]
    fondos: list[FondoTema]
    cierres: list[CierreDia]
    embudo: list[EtapaEmbudo]  # empty until the CRM funnel exists (plan N14)
