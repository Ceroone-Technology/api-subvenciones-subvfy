"""Autenticación (quién eres) y autorización (qué puedes hacer).

El modelo de permisos acordado, sobre los tres roles del catálogo:

| Rol       | Alcance                                                       |
|-----------|---------------------------------------------------------------|
| `admin`   | Global: cualquier empresa y cualquier usuario.                |
| `gestor`  | Lectura y escritura, pero solo dentro de su propia empresa.   |
| `usuario` | Lee su empresa y edita únicamente su propio perfil.           |

La regla transversal, y la que de verdad importa en un producto B2B
multi-tenant: **nadie que no sea admin ve datos de otra empresa**. Los
listados no se limitan a rechazar un `empresa_id` ajeno, sino que fuerzan
el filtro a la empresa del token (`filtro_empresa`), para que un fallo al
añadir un endpoint nuevo no se traduzca en una fuga entre clientes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TIPO_ACCESS, TokenInvalido, leer_token
from app.database import get_db
from app.models import Rol, Usuario

ROL_ADMIN = "admin"
ROL_GESTOR = "gestor"
ROL_USUARIO = "usuario"

# auto_error=False para poder devolver siempre el mismo 401 con cabecera
# WWW-Authenticate, tanto si falta la cabecera como si el token es inválido.
_bearer = HTTPBearer(auto_error=False, description="Access token obtenido en POST /auth/login.")

_NO_AUTENTICADO = HTTPException(
    status.HTTP_401_UNAUTHORIZED,
    detail="Credenciales ausentes o inválidas.",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True)
class UsuarioAutenticado:
    """El usuario del token, ya resuelto contra la base de datos, junto con
    el código de su rol (el token solo lleva el id: así un cambio de rol o
    un bloqueo surten efecto en la siguiente petición)."""

    usuario: Usuario
    rol: str

    @property
    def id(self) -> int:
        return self.usuario.id

    @property
    def empresa_id(self) -> int:
        return self.usuario.empresa_id

    @property
    def es_admin(self) -> bool:
        return self.rol == ROL_ADMIN

    def puede_ver_empresa(self, empresa_id: int) -> bool:
        return self.es_admin or empresa_id == self.empresa_id

    def exigir_acceso_a_empresa(self, empresa_id: int) -> None:
        """404 y no 403 a propósito: a un cliente no se le confirma que
        exista una empresa de otro cliente. La respuesta es indistinguible
        de la de un id inexistente."""
        if not self.puede_ver_empresa(empresa_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada.")

    @property
    def filtro_empresa(self) -> int | None:
        """`empresa_id` al que hay que restringir los listados, o None si es
        admin y puede verlo todo."""
        return None if self.es_admin else self.empresa_id


async def usuario_actual(
    credenciales: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UsuarioAutenticado:
    if credenciales is None:
        raise _NO_AUTENTICADO
    try:
        usuario_id = leer_token(credenciales.credentials, TIPO_ACCESS)
    except TokenInvalido as exc:
        raise _NO_AUTENTICADO from exc

    fila = (
        await db.execute(
            select(Usuario, Rol.codigo).join(Rol, Rol.id == Usuario.rol_id).where(Usuario.id == usuario_id)
        )
    ).first()
    if fila is None:
        raise _NO_AUTENTICADO

    usuario, rol_codigo = fila
    if usuario.estado != "activo":
        # El token sigue siendo criptográficamente válido, pero la cuenta ya
        # no: bloquear a alguien tiene efecto inmediato, no cuando caduque.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"La cuenta está en estado '{usuario.estado}'."
        )
    return UsuarioAutenticado(usuario=usuario, rol=rol_codigo)


UsuarioActualDep = Annotated[UsuarioAutenticado, Depends(usuario_actual)]


def exigir_roles(*roles: str) -> Callable[[UsuarioAutenticado], UsuarioAutenticado]:
    """Dependencia que exige uno de los roles indicados.

    Se usa como `Depends(exigir_roles(ROL_ADMIN))` en el endpoint, o en
    `dependencies=[...]` cuando no hace falta el usuario dentro del handler.
    """

    def comprobar(actual: UsuarioActualDep) -> UsuarioAutenticado:
        if actual.rol not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"Requiere uno de estos roles: {', '.join(roles)}.",
            )
        return actual

    return comprobar


AdminDep = Annotated[UsuarioAutenticado, Depends(exigir_roles(ROL_ADMIN))]
GestorDep = Annotated[UsuarioAutenticado, Depends(exigir_roles(ROL_ADMIN, ROL_GESTOR))]
