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
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.models.empresa import (
    ESTADOS_EMPRESA,
    MAX_EMPLEADOS,
    MAX_PROYECTO_PREVISTO,
    TAMANOS_VALIDOS,
    TRAMOS_FACTURACION,
)
from app.models.interes import CODIGOS_INTERES
from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA, CODIGOS_ACTIVIDAD

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí los ignore.
TamanoEmpresa = Literal[TAMANOS_VALIDOS]  # type: ignore[valid-type]
EstadoEmpresa = Literal[ESTADOS_EMPRESA]  # type: ignore[valid-type]
CodigoActividad = Literal[CODIGOS_ACTIVIDAD]  # type: ignore[valid-type]
TramoFacturacion = Literal[TRAMOS_FACTURACION]  # type: ignore[valid-type]
CodigoInteres = Literal[CODIGOS_INTERES]  # type: ignore[valid-type]


def _normalizar_nif(valor: str) -> str:
    # El NIF es UNIQUE en base de datos: sin normalizar, "b12345678" y
    # "B12345678" crearían dos empresas para el mismo tenant.
    return valor.strip().upper()


def _normalizar_texto(valor: str | None) -> str | None:
    return None if valor is None else valor.strip()


Nif = Annotated[str, Field(min_length=1, max_length=15), AfterValidator(_normalizar_nif)]
RazonSocial = Annotated[str, Field(min_length=1, max_length=200)]
ActividadOtra = Annotated[str, Field(max_length=200), AfterValidator(_normalizar_texto)]
# Extended profile (N2): trimmed; an empty string is "not filled in" and is stored as NULL.
def _texto_o_none(valor: str) -> str | None:
    return valor.strip() or None


Provincia = Annotated[str, Field(max_length=60), AfterValidator(_texto_o_none)]
Municipio = Annotated[str, Field(max_length=100), AfterValidator(_texto_o_none)]
ProyectoPrevisto = Annotated[str, Field(max_length=MAX_PROYECTO_PREVISTO), AfterValidator(_texto_o_none)]
MAX_SITIO_WEB = 200


def _sitio_web_valido(valor: str) -> str | None:
    """Only absolute http(s) URLs with a host; empty clears it. The table CHECK repeats the rule."""
    valor = valor.strip()
    if not valor:
        return None
    try:
        partes = urlsplit(valor)
        valido = (
            partes.scheme.lower() in ("http", "https")
            and bool(partes.hostname)
            and not any(c.isspace() or ord(c) < 32 for c in valor)
        )
    except ValueError:
        valido = False
    if not valido:
        raise ValueError("El sitio web debe ser una dirección http:// o https:// completa.")
    return valor


SitioWeb = Annotated[str, Field(max_length=MAX_SITIO_WEB), AfterValidator(_sitio_web_valido)]
Empleados = Annotated[int, Field(ge=0, le=MAX_EMPLEADOS)]
# Each interest at most once; the order is not kept (the catalog order is used on read).
Intereses = Annotated[list[CodigoInteres], Field(max_length=len(CODIGOS_INTERES))]


def _sin_repetidos(valores: list[str]) -> list[str]:
    if len(set(valores)) != len(valores):
        raise ValueError("Hay intereses repetidos.")
    return valores


class PerfilAmpliado(BaseModel):
    """Optional fields of the extended profile, shared by create and update."""

    facturacion_tramo: TramoFacturacion | None = None
    empleados: Empleados | None = None
    provincia: Provincia | None = None
    municipio: Municipio | None = None
    proyecto_previsto: ProyectoPrevisto | None = None
    sitio_web: SitioWeb | None = None
    intereses: Annotated[Intereses, AfterValidator(_sin_repetidos)] | None = None

MENSAJE_OTRA_SIN_TEXTO = "Con la actividad 'OTRA' hay que indicar cuál es."


class EmpresaBase(BaseModel):
    razon_social: RazonSocial
    nif: Nif
    sector: str | None = Field(default=None, max_length=100)
    tamano: TamanoEmpresa | None = None
    ccaa: str | None = Field(default=None, max_length=100)
    descripcion: str | None = None


class EmpresaCreate(EmpresaBase, PerfilAmpliado):
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


class EmpresaUpdate(PerfilAmpliado):
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

    @model_validator(mode="after")
    def _intereses_no_se_anulan(self) -> Self:
        # `[]` los vacía; `null` no significa nada.
        if "intereses" in self.model_fields_set and self.intereses is None:
            raise ValueError("Los intereses no pueden ser null: envía [] para vaciarlos.")
        return self


class ItemPerfilRead(BaseModel):
    codigo: str
    etiqueta: str


class EmpresaRead(EmpresaBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    estado: EstadoEmpresa
    actividad_codigo: CodigoActividad | None
    actividad_otra: str | None
    facturacion_tramo: TramoFacturacion | None
    empleados: int | None
    provincia: str | None
    municipio: str | None
    proyecto_previsto: str | None
    sitio_web: str | None
    # Versioned URL of the authenticated GET, or null.
    logo_url: str | None
    # Codes of `GET /catalogos/intereses`, in catalog order.
    intereses: list[str]
    # Derivados del modelo, no almacenados.
    actividad_pendiente: bool
    ofrecer_ia: bool
    perfil_completo_pct: int
    perfil_pendiente: list[ItemPerfilRead]
    created_at: datetime
    updated_at: datetime


class InteresRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    codigo: str
    nombre: str
    orden: int


class EmpresaSesion(BaseModel):
    """Lo mínimo de la empresa que `/auth/me` necesita para que el front pida la actividad sin otra llamada."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    actividad_pendiente: bool
    ofrecer_ia: bool
    logo_url: str | None = None


class SectorActividadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    codigo: str
    descripcion: str
    orden: int
