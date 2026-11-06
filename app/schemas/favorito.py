"""Schemas de favorito."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.convocatoria import ConvocatoriaRead, ConvocatoriaUpsert, TemaConvocatoria
from app.services.convocatoria_estado import EstadoConvocatoria

# La nota es un Text en base de datos, pero un límite explícito evita que un
# copia-pega accidental de media convocatoria entre por aquí.
Nota = Field(default=None, max_length=2000)


class FavoritoCreate(BaseModel):
    """Marcar un favorito lleva la ficha de la convocatoria dentro: es lo que
    permite sembrar la caché local en el mismo paso, sin obligar al frontend
    a conocer ningún id nuestro."""

    convocatoria: ConvocatoriaUpsert
    nota: str | None = Nota


class FavoritoUpdate(BaseModel):
    nota: str | None = Nota


class ConvocatoriaFavorita(ConvocatoriaRead):
    """The cached call plus what a favorites card needs (plan N7): deadline, budget, state and own topic.

    `estado` and `dias_restantes` are computed in SQL against today in Madrid, never stored.
    `importe_max` (per-project cap) will come with the AI summary (E-B6a); until then the card shows the budget.
    """

    fecha_inicio_solicitud: date | None
    fecha_fin_solicitud: date | None
    estado: EstadoConvocatoria
    plazo_permanente: bool
    presupuesto_total: float | None
    tema: TemaConvocatoria
    gestiona_subvfy: bool
    dias_restantes: int | None


class FavoritoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nota: str | None
    created_at: datetime
    updated_at: datetime
    # Resuelta por join: el listado de favoritos se pinta sin volver a la BDNS.
    convocatoria: ConvocatoriaFavorita


class PorPlazo(BaseModel):
    """Bar by deadline of the favorites summary. The five groups add up to `total`."""

    vencidas: int
    hasta_7_dias: int
    de_8_a_30_dias: int
    mas_de_30_dias: int
    sin_fecha: int


class FavoritosResumen(BaseModel):
    total: int
    # Sum of the budgets of the saved calls that have one.
    importe_total: float
    cierran_en_14_dias: int
    con_nota: int
    por_plazo: PorPlazo
