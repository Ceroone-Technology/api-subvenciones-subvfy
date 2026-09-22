"""Schemas de alerta.

Los filtros de órgano y región son listas de **ids del catálogo de la BDNS**
(no texto): el frontend los tiene a mano porque consulta la BDNS
directamente, y así no hay que mantener aquí una copia del catálogo. Se
normalizan en la entrada con `normalizar_ids_bdns`, la única fuente de verdad
de esa regla.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.alerta import CANALES_NOTIFICACION, FRECUENCIAS_VALIDAS, NIVELES_ADMINISTRACION
from app.services.filtros import normalizar_ids_bdns

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí los ignore.
NivelAdministracion = Literal[NIVELES_ADMINISTRACION]  # type: ignore[valid-type]
Frecuencia = Literal[FRECUENCIAS_VALIDAS]  # type: ignore[valid-type]
CanalNotificacion = Literal[CANALES_NOTIFICACION]  # type: ignore[valid-type]

# Las columnas organo_bdns_id/region_bdns_id son INTEGER, no BIGINT: sin el
# techo, un id enorme pasaría la validación y reventaría en el INSERT.
IdBdns = Annotated[int, Field(gt=0, le=2_147_483_647)]
# Un tope generoso: una alerta con más filtros que esto no filtra nada.
MAX_FILTROS = 100

_CAMPOS_FILTRO = ("organos", "regiones")


def _comprobar_rango(desde: date | None, hasta: date | None) -> None:
    if desde is not None and hasta is not None and desde > hasta:
        raise ValueError(f"fecha_desde ({desde}) no puede ser posterior a fecha_hasta ({hasta}).")


class AlertaCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=150)
    texto_busqueda: str | None = Field(default=None, max_length=300)
    nivel_administracion: NivelAdministracion | None = None
    fecha_desde: date | None = None
    fecha_hasta: date | None = None
    solo_mrr: bool = False
    frecuencia: Frecuencia = "diaria"
    canal_notificacion: CanalNotificacion = "email"
    activa: bool = True
    organos: list[IdBdns] = Field(default_factory=list, max_length=MAX_FILTROS)
    regiones: list[IdBdns] = Field(default_factory=list, max_length=MAX_FILTROS)

    @field_validator(*_CAMPOS_FILTRO)
    @classmethod
    def _normalizar_filtros(cls, ids: list[int]) -> list[int]:
        return normalizar_ids_bdns(ids)

    @model_validator(mode="after")
    def _rango_de_fechas(self) -> Self:
        # Validarlo aquí da un 422 claro; dejarlo al CHECK de la tabla daría un 500.
        _comprobar_rango(self.fecha_desde, self.fecha_hasta)
        return self


class AlertaUpdate(BaseModel):
    """PATCH parcial: solo se aplica lo que viene en el body.

    En `organos`/`regiones`, ausente = no se toca, `[]` = se vacía y una
    lista **reemplaza** la anterior (no se añade a ella).
    """

    nombre: str | None = Field(default=None, min_length=1, max_length=150)
    texto_busqueda: str | None = Field(default=None, max_length=300)
    nivel_administracion: NivelAdministracion | None = None
    fecha_desde: date | None = None
    fecha_hasta: date | None = None
    solo_mrr: bool | None = None
    frecuencia: Frecuencia | None = None
    canal_notificacion: CanalNotificacion | None = None
    activa: bool | None = None
    organos: list[IdBdns] | None = Field(default=None, max_length=MAX_FILTROS)
    regiones: list[IdBdns] | None = Field(default=None, max_length=MAX_FILTROS)

    @field_validator(
        "nombre", "solo_mrr", "frecuencia", "canal_notificacion", "activa", *_CAMPOS_FILTRO, mode="before"
    )
    @classmethod
    def _sin_null(cls, valor: Any) -> Any:
        # Opcionales para poder omitirlos, pero no se pueden vaciar: son NOT
        # NULL en la tabla. Para quitar todos los filtros se manda `[]`.
        if valor is None:
            raise ValueError("no admite null; omite el campo para no modificarlo.")
        return valor

    @field_validator(*_CAMPOS_FILTRO)
    @classmethod
    def _normalizar_filtros(cls, ids: list[int] | None) -> list[int] | None:
        return None if ids is None else normalizar_ids_bdns(ids)

    @model_validator(mode="after")
    def _rango_de_fechas(self) -> Self:
        # Solo compara lo que viene en el body; el servicio vuelve a
        # comprobarlo contra los valores ya guardados.
        _comprobar_rango(self.fecha_desde, self.fecha_hasta)
        return self


class AlertaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    usuario_id: int
    nombre: str
    texto_busqueda: str | None
    nivel_administracion: str | None
    fecha_desde: date | None
    fecha_hasta: date | None
    solo_mrr: bool
    frecuencia: str
    canal_notificacion: str
    activa: bool
    organos: list[int]
    regiones: list[int]
    ultima_ejecucion_at: datetime | None
    created_at: datetime
    updated_at: datetime
