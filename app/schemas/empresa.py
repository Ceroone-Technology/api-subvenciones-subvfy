"""Schemas de empresa (el tenant).

Los valores válidos de `tamano`/`estado` se importan del modelo en vez de
repetirse aquí: son las mismas tuplas que alimentan los CHECK de la tabla,
y `Literal[tupla]` las expande a un enum real en el OpenAPI que consume el
frontend.

Actividad (`empresa-actividad-ia`): obligatoria al crear; en el PATCH no se
puede dejar a null. `OTRA` exige texto libre, y cualquier otro código lo
descarta. La regla vive aquí y en el CHECK de la tabla.
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.models.empresa import ESTADOS_EMPRESA, TAMANOS_VALIDOS
from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA, CODIGOS_ACTIVIDAD

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí los ignore.
TamanoEmpresa = Literal[TAMANOS_VALIDOS]  # type: ignore[valid-type]
EstadoEmpresa = Literal[ESTADOS_EMPRESA]  # type: ignore[valid-type]
CodigoActividad = Literal[CODIGOS_ACTIVIDAD]  # type: ignore[valid-type]


def _normalizar_nif(valor: str) -> str:
    # El NIF es UNIQUE en base de datos: sin normalizar, "b12345678" y
    # "B12345678" crearían dos empresas para el mismo tenant.
    return valor.strip().upper()


def _normalizar_texto(valor: str | None) -> str | None:
    return None if valor is None else valor.strip()


Nif = Annotated[str, Field(min_length=1, max_length=15), AfterValidator(_normalizar_nif)]
RazonSocial = Annotated[str, Field(min_length=1, max_length=200)]
ActividadOtra = Annotated[str, Field(max_length=200), AfterValidator(_normalizar_texto)]

MENSAJE_OTRA_SIN_TEXTO = "Con la actividad 'OTRA' hay que indicar cuál es."


class EmpresaBase(BaseModel):
    razon_social: RazonSocial
    nif: Nif
    sector: str | None = Field(default=None, max_length=100)
    tamano: TamanoEmpresa | None = None
    ccaa: str | None = Field(default=None, max_length=100)
    descripcion: str | None = None


class EmpresaCreate(EmpresaBase):
    estado: EstadoEmpresa = "activa"
    actividad_codigo: CodigoActividad
    actividad_otra: ActividadOtra | None = None

    @model_validator(mode="after")
    def _coherencia_de_actividad(self) -> Self:
        if self.actividad_codigo == CODIGO_ACTIVIDAD_OTRA:
            if not self.actividad_otra:
                raise ValueError(MENSAJE_OTRA_SIN_TEXTO)
        else:
            self.actividad_otra = None
        return self


class EmpresaUpdate(BaseModel):
    """PATCH: solo se aplican los campos presentes en el body."""

    razon_social: RazonSocial | None = None
    nif: Nif | None = None
    sector: str | None = Field(default=None, max_length=100)
    tamano: TamanoEmpresa | None = None
    ccaa: str | None = Field(default=None, max_length=100)
    descripcion: str | None = None
    estado: EstadoEmpresa | None = None
    actividad_codigo: CodigoActividad | None = None
    actividad_otra: ActividadOtra | None = None

    @model_validator(mode="after")
    def _actividad_no_se_anula(self) -> Self:
        # Presente y null no es "no tocar": dejaría la empresa otra vez pendiente.
        if "actividad_codigo" in self.model_fields_set and self.actividad_codigo is None:
            raise ValueError("La actividad no se puede dejar vacía.")
        if self.actividad_codigo == CODIGO_ACTIVIDAD_OTRA and not self.actividad_otra:
            raise ValueError(MENSAJE_OTRA_SIN_TEXTO)
        return self


class EmpresaRead(EmpresaBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    estado: EstadoEmpresa
    actividad_codigo: CodigoActividad | None
    actividad_otra: str | None
    # Derivados del modelo, no almacenados.
    actividad_pendiente: bool
    ofrecer_ia: bool
    created_at: datetime
    updated_at: datetime


class EmpresaSesion(BaseModel):
    """Lo mínimo de la empresa que `/auth/me` necesita para que el front pida la actividad sin otra llamada."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    actividad_pendiente: bool
    ofrecer_ia: bool


class SectorActividadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    codigo: str
    descripcion: str
    orden: int
