from datetime import datetime

from pydantic import BaseModel


class InicioResumen(BaseModel):
    """`GET /inicio/resumen`: the four counters of the Inicio page."""

    novedades: int
    cierran_7_dias: int
    alertas_con_resultados: int
    en_seguimiento: int
    # Start of the "novedades" window: previous access of the user (clamped to 30 days; 7 days when there is none).
    novedades_desde: datetime
