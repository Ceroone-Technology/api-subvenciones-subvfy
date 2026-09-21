"""Schemas de empresa (el tenant).

Los valores válidos de `tamano`/`estado` se importan del modelo en vez de
repetirse aquí: son las mismas tuplas que alimentan los CHECK de la tabla,
y `Literal[tupla]` las expande a un enum real en el OpenAPI que consume el
frontend.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.empresa import ESTADOS_EMPRESA, TAMANOS_VALIDOS

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí los ignore.
TamanoEmpresa = Literal[TAMANOS_VALIDOS]  # type: ignore[valid-type]
EstadoEmpresa = Literal[ESTADOS_EMPRESA]  # type: ignore[valid-type]


def _normalizar_nif(valor: str) -> str:
    # El NIF es UNIQUE en base de datos: sin normalizar, "b12345678" y
    # "B12345678" crearían dos empresas para el mismo tenant.
    return valor.strip().upper()


Nif = Annotated[str, Field(min_length=1, max_length=15), AfterValidator(_normalizar_nif)]
RazonSocial = Annotated[str, Field(min_length=1, max_length=200)]


class EmpresaBase(BaseModel):
    razon_social: RazonSocial
    nif: Nif
    sector: str | None = Field(default=None, max_length=100)
    tamano: TamanoEmpresa | None = None
    ccaa: str | None = Field(default=None, max_length=100)
    descripcion: str | None = None


class EmpresaCreate(EmpresaBase):
    estado: EstadoEmpresa = "activa"


class EmpresaUpdate(BaseModel):
    """PATCH: solo se aplican los campos presentes en el body."""

    razon_social: RazonSocial | None = None
    nif: Nif | None = None
    sector: str | None = Field(default=None, max_length=100)
    tamano: TamanoEmpresa | None = None
    ccaa: str | None = Field(default=None, max_length=100)
    descripcion: str | None = None
    estado: EstadoEmpresa | None = None


class EmpresaRead(EmpresaBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    estado: EstadoEmpresa
    created_at: datetime
    updated_at: datetime
