"""Schemas de usuario.

`password_hash` no aparece en ningún schema de lectura — el hash nunca sale
de la API. En escritura se recibe `password` en claro y el router la hashea
con `app.core.security` antes de persistir.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import PASSWORD_MAX_BYTES, PASSWORD_MIN_LONGITUD
from app.models.usuario import ESTADOS_USUARIO

EstadoUsuario = Literal[ESTADOS_USUARIO]

# bcrypt corta a 72 bytes: mejor rechazar con un 422 explícito que aceptar
# una contraseña que en realidad se trunca sin avisar.
Password = Annotated[
    str,
    Field(
        min_length=PASSWORD_MIN_LONGITUD,
        max_length=PASSWORD_MAX_BYTES,
        description=f"Entre {PASSWORD_MIN_LONGITUD} y {PASSWORD_MAX_BYTES} caracteres.",
    ),
]
Nombre = Annotated[str, Field(min_length=1, max_length=100)]
Apellidos = Annotated[str, Field(min_length=1, max_length=150)]


class UsuarioBase(BaseModel):
    empresa_id: int
    rol_id: int
    nombre: Nombre
    apellidos: Apellidos
    email: EmailStr = Field(max_length=200)


class UsuarioCreate(UsuarioBase):
    password: Password
    estado: EstadoUsuario = "activo"


class UsuarioUpdate(BaseModel):
    """PATCH: solo se aplican los campos presentes en el body."""

    empresa_id: int | None = None
    rol_id: int | None = None
    nombre: Nombre | None = None
    apellidos: Apellidos | None = None
    email: EmailStr | None = Field(default=None, max_length=200)
    password: Password | None = None
    estado: EstadoUsuario | None = None


class UsuarioRead(UsuarioBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    estado: EstadoUsuario
    ultimo_acceso_at: datetime | None
    created_at: datetime
    updated_at: datetime
