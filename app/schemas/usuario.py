"""Schemas de usuario.

`password_hash` no aparece en ningún schema de lectura — el hash nunca sale
de la API. En escritura se recibe `password` en claro y el router la hashea
con `app.core.security` antes de persistir.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, EmailStr, Field

from app.core.security import PASSWORD_MAX_BYTES, PASSWORD_MIN_LONGITUD
from app.models.usuario import ESTADOS_USUARIO

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí el ignore.
EstadoUsuario = Literal[ESTADOS_USUARIO]  # type: ignore[valid-type]


def _cabe_en_bcrypt(password: str) -> str:
    """bcrypt admite 72 **bytes**, y `max_length` cuenta caracteres: 40 eñes son
    40 caracteres y 80 bytes. Antes passlib las truncaba en silencio; con
    bcrypt 5 reventarían. Se rechaza con un 422 explícito."""
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(
            f"La contraseña no puede ocupar más de {PASSWORD_MAX_BYTES} bytes en UTF-8 "
            "(las letras con tilde y la ñ ocupan dos)."
        )
    return password


# max_length se queda para el OpenAPI y como cota barata; el límite real es
# el de bytes del validador.
Password = Annotated[
    str,
    Field(
        min_length=PASSWORD_MIN_LONGITUD,
        max_length=PASSWORD_MAX_BYTES,
        description=(
            f"Entre {PASSWORD_MIN_LONGITUD} caracteres y {PASSWORD_MAX_BYTES} bytes en UTF-8 "
            "(las letras con tilde y la ñ ocupan dos)."
        ),
    ),
    AfterValidator(_cabe_en_bcrypt),
]
Nombre = Annotated[str, Field(min_length=1, max_length=100)]
_TELEFONO = re.compile(r"^\+?[0-9 ()\-.]{6,30}$")


def _normalizar_telefono(valor: object) -> object:
    """Strips blanks; an empty string clears the phone. The format is the one the table CHECK repeats."""
    if isinstance(valor, str):
        valor = valor.strip()
        return valor or None
    return valor


def _validar_telefono(telefono: str | None) -> str | None:
    if telefono is None:
        return None
    digitos = sum(c.isdigit() for c in telefono)
    if not _TELEFONO.fullmatch(telefono) or not 6 <= digitos <= 15:
        raise ValueError("Teléfono no válido: de 6 a 15 cifras; admite +, espacios, puntos, guiones y paréntesis.")
    return telefono


Telefono = Annotated[str | None, BeforeValidator(_normalizar_telefono), AfterValidator(_validar_telefono)]
Apellidos = Annotated[str, Field(min_length=1, max_length=150)]


class UsuarioBase(BaseModel):
    empresa_id: int
    rol_id: int
    nombre: Nombre
    apellidos: Apellidos
    email: EmailStr = Field(max_length=200)
    telefono: Telefono = None


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
    telefono: Telefono = None
    password: Password | None = None
    estado: EstadoUsuario | None = None


class UsuarioRead(UsuarioBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    estado: EstadoUsuario
    ultimo_acceso_at: datetime | None
    created_at: datetime
    updated_at: datetime
