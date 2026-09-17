"""Schemas de login, refresh y sesión."""

from pydantic import BaseModel, EmailStr, Field

from app.core.security import PASSWORD_MAX_BYTES
from app.schemas.usuario import UsuarioRead


class LoginRequest(BaseModel):
    email: EmailStr
    # Sin min_length: en el login no se validan reglas de contraseña, solo
    # se comprueba si coincide. Un 422 aquí le diría a quien prueba
    # credenciales que esa contraseña ni siquiera merecía consulta.
    password: str = Field(max_length=PASSWORD_MAX_BYTES)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = Field(description="Segundos de validez del access_token.")


class SesionResponse(TokenResponse):
    """Respuesta del login: los tokens y, de paso, el usuario — así el
    frontend pinta la sesión sin encadenar una segunda llamada a /auth/me."""

    usuario: UsuarioRead
