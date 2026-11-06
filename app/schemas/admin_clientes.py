"""Schemas of the admin client panel. No password, hash or token is ever part of these."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.seguimiento import ESTADOS_SEGUIMIENTO
from app.schemas.empresa import EmpresaRead
from app.schemas.usuario import EstadoUsuario

RolCodigo = Literal["admin", "gestor", "usuario"]


class AdminUsuarioRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    apellidos: str
    email: str
    empresa_id: int
    empresa: str
    rol: str
    estado: str
    ultimo_acceso_at: datetime | None
    created_at: datetime


class AdminUsuarioUpdate(BaseModel):
    """Role and status only. Presence-based: at least one of them."""

    model_config = ConfigDict(extra="forbid")

    rol: RolCodigo | None = None
    estado: EstadoUsuario | None = None


class UsuarioResumen(BaseModel):
    id: int
    nombre: str
    apellidos: str
    email: str
    rol: str
    estado: str
    ultimo_acceso_at: datetime | None


class FavoritoResumen(BaseModel):
    usuario_id: int
    codigo_bdns: str
    titulo: str
    created_at: datetime


class SeguimientoPorEstado(BaseModel):
    estado: Literal[ESTADOS_SEGUIMIENTO]  # type: ignore[valid-type]
    total: int


class AlertasResumen(BaseModel):
    total: int
    activas: int


class AdminEmpresaDetalle(BaseModel):
    empresa: EmpresaRead
    usuarios: list[UsuarioResumen]
    seguimiento: list[SeguimientoPorEstado]
    favoritos_total: int
    favoritos: list[FavoritoResumen] = Field(description="The 20 most recent.")
    alertas: AlertasResumen
    # Filled once the management requests (N14) exist.
    solicitudes_recientes: list[dict[str, object]]
