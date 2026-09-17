"""Schemas Pydantic de request/response, uno por recurso.

Se añaden junto con los endpoints de cada Funcionalidad del backlog.
"""

from app.schemas.auth import LoginRequest, RefreshRequest, SesionResponse, TokenResponse
from app.schemas.common import Pagina
from app.schemas.convocatoria import ConvocatoriaRead, ConvocatoriaUpsert
from app.schemas.empresa import EmpresaCreate, EmpresaRead, EmpresaUpdate
from app.schemas.favorito import FavoritoCreate, FavoritoRead, FavoritoUpdate
from app.schemas.rol import RolRead
from app.schemas.usuario import UsuarioCreate, UsuarioRead, UsuarioUpdate

__all__ = [
    "ConvocatoriaRead",
    "ConvocatoriaUpsert",
    "EmpresaCreate",
    "EmpresaRead",
    "EmpresaUpdate",
    "FavoritoCreate",
    "FavoritoRead",
    "FavoritoUpdate",
    "LoginRequest",
    "Pagina",
    "RefreshRequest",
    "SesionResponse",
    "TokenResponse",
    "RolRead",
    "UsuarioCreate",
    "UsuarioRead",
    "UsuarioUpdate",
]
