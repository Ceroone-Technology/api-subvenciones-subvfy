"""Autenticación (quién eres) y autorización (qué puedes hacer).

El modelo de permisos acordado, sobre los tres roles del catálogo:

| Rol       | Alcance                                                       |
|-----------|---------------------------------------------------------------|
| `admin`   | Global: cualquier empresa y cualquier usuario.                |
| `gestor`  | Su empresa, y solo sobre usuarios con rol `usuario`.          |
| `usuario` | Lee su empresa y edita únicamente su propio perfil.           |

Sobre el recurso `usuario` hay tres reglas más, que cierran la escalada de
privilegios de la auditoría del 06/10 (AUD-001 y AUD-002):

- **`rol_id` y `empresa_id` son solo de admin.** Un gestor que los envíe
  recibe 403, aunque sea su propia empresa o el valor que ya tenía: quien
  puede repartir roles o mover gente entre empresas es admin y nadie más.
- **Un gestor gestiona objetivos con rol `usuario`**, más su propio perfil.
  Sobre un admin o otro gestor de su empresa recibe 403, contraseña
  incluida; si no, bastaba con compartir empresa con un admin para cambiarle
  la contraseña y entrar con ella.
- **Nadie se cambia su propio rol ni su propio estado**, admin incluido, ni
  se da de baja a sí mismo. Si el único admin se degrada o se bloquea, ya no
  hay quien lo arregle por la API.

El orden de comprobación importa: **primero el tenant, después el rol**. Un
objetivo de otra empresa da 404 sin mirar su rol, porque un 403 confirmaría
que ese id existe.

**La baja de una empresa corta el acceso de toda su gente** (AUD-009). Lo
decide `motivo_cuenta_no_operativa`, una función pura que no sabe de HTTP:
cada puerta de entrada la llama y traduce el motivo a su código, porque el
login y el refresh no pasan por `usuario_actual` y la regla tiene que ser la
misma en los tres sitios. Desde entonces, el `estado` de una empresa no es un
campo cualquiera: ponerlo en `inactiva` deja fuera a todo el tenant en la
petición siguiente, así que solo lo cambia un admin y nadie da de baja la
empresa en la que vive.

La regla transversal, y la que de verdad importa en un producto B2B
multi-tenant: **nadie que no sea admin ve datos de otra empresa**. Los
listados no se limitan a rechazar un `empresa_id` ajeno, sino que fuerzan
el filtro a la empresa del token (`filtro_empresa`), para que un fallo al
añadir un endpoint nuevo no se traduzca en una fuga entre clientes.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TIPO_ACCESS, TokenInvalido, leer_token
from app.database import get_db
from app.models import Empresa, Rol, Usuario

ROL_ADMIN = "admin"
ROL_GESTOR = "gestor"
ROL_USUARIO = "usuario"

# Campos del recurso `usuario` con dueño: `CAMPOS_SOLO_ADMIN` reparte
# privilegios y tenants, así que no los toca ningún gestor;
# `CAMPOS_PROPIOS_BLOQUEADOS` son los que nadie puede cambiarse a sí mismo; y
# `CAMPOS_DE_GESTION` es lo que no puede tocarse quien no gestiona a nadie.
ESTADO_USUARIO_ACTIVO = "activo"
ESTADO_EMPRESA_ACTIVA = "activa"

CAMPOS_SOLO_ADMIN = ("rol_id", "empresa_id")
CAMPOS_PROPIOS_BLOQUEADOS = ("rol_id", "estado")
CAMPOS_DE_GESTION = ("empresa_id", "rol_id", "estado")

# Y en el recurso `empresa`, `estado` es solo de admin desde que la baja corta
# el acceso de todo el tenant: no es una preferencia del cliente.
CAMPOS_EMPRESA_SOLO_ADMIN = ("estado",)

# auto_error=False para poder devolver siempre el mismo 401 con cabecera
# WWW-Authenticate, tanto si falta la cabecera como si el token es inválido.
_bearer = HTTPBearer(auto_error=False, description="Access token obtenido en POST /auth/login.")

_NO_AUTENTICADO = HTTPException(
    status.HTTP_401_UNAUTHORIZED,
    detail="Credenciales ausentes o inválidas.",
    headers={"WWW-Authenticate": "Bearer"},
)


def motivo_cuenta_no_operativa(estado_usuario: str, estado_empresa: str) -> str | None:
    """Por qué esta cuenta no puede operar, o `None` si puede.

    Pura y sin `HTTPException` a propósito: la usan tres puertas de entrada
    que responden distinto —`usuario_actual` y el login con 403 y el motivo,
    el refresh con su 401 genérico—, así que el criterio vive aquí una sola
    vez y la traducción a HTTP es de cada una.

    La empresa se mira primero porque es la causa más amplia: si el tenant
    está dado de baja, el estado de la cuenta es lo de menos y lo que hay que
    arreglar es la empresa.
    """
    if estado_empresa != ESTADO_EMPRESA_ACTIVA:
        return "La empresa está dada de baja."
    if estado_usuario != ESTADO_USUARIO_ACTIVO:
        return f"La cuenta está en estado '{estado_usuario}'."
    return None


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

    def exigir_gestion_de(self, objetivo_id: int, rol_objetivo: str) -> None:
        """¿Puede el usuario actual gestionar a este otro usuario?

        El tenant lo comprueba antes quien llama (`exigir_acceso_a_empresa`),
        así que aquí solo queda el rol. Un admin gestiona a cualquiera; un
        gestor, solo a usuarios con rol `usuario`; quien no es ni lo uno ni lo
        otro no gestiona a nadie. El propio perfil siempre se permite, y los
        campos que puede tocar los decide `exigir_campos_permitidos`.

        La regla se enuncia entera aquí aunque hoy el router frene antes al rol
        `usuario`: una función de autorización no debe depender de quién la
        llame para denegar.
        """
        if objetivo_id == self.id or self.es_admin:
            return
        if self.rol != ROL_GESTOR:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail="Solo puedes editar tu propio perfil."
            )
        if rol_objetivo != ROL_USUARIO:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"No puedes gestionar a un usuario con rol '{rol_objetivo}'.",
            )

    def exigir_rol_asignable(self, rol_codigo: str) -> None:
        """Qué rol puede asignar al crear o editar. Solo admin reparte roles de
        gestión: si no, un gestor podría crearse un admin y entrar con él."""
        if not self.es_admin and rol_codigo != ROL_USUARIO:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"Solo un admin puede asignar el rol '{rol_codigo}'.",
            )

    def exigir_campos_permitidos(self, campos: Iterable[str], *, objetivo_id: int) -> None:
        """403 nombrando los campos que no puede tocar, para que el cliente
        sepa qué quitar del body.

        Se decide por **presencia**, no por valor: reenviar `rol_id` con el
        que ya tiene también se rechaza. Es la semántica de un PATCH, y evita
        que el permiso dependa del estado de la fila.
        """
        enviados = set(campos)
        prohibidos: set[str] = set()
        if not self.es_admin:
            prohibidos |= enviados & set(CAMPOS_SOLO_ADMIN)
            if self.rol != ROL_GESTOR:
                prohibidos |= enviados & set(CAMPOS_DE_GESTION)
        if objetivo_id == self.id:
            prohibidos |= enviados & set(CAMPOS_PROPIOS_BLOQUEADOS)
        if prohibidos:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"No puedes modificar estos campos: {', '.join(sorted(prohibidos))}.",
            )

    def exigir_campos_de_empresa_permitidos(self, campos: Iterable[str]) -> None:
        """`estado` de empresa solo lo cambia un admin, por presencia del campo
        y no por su valor: ni para dar de baja ni para reactivar, porque
        tampoco es del gestor la decisión de devolver el acceso."""
        if self.es_admin:
            return
        prohibidos = set(campos) & set(CAMPOS_EMPRESA_SOLO_ADMIN)
        if prohibidos:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"No puedes modificar estos campos: {', '.join(sorted(prohibidos))}.",
            )

    def exigir_no_es_su_propia_empresa(self, empresa_id: int) -> None:
        """Nadie da de baja la empresa en la que vive.

        Se rechaza **el valor que deja fuera**, no el campo: un admin puede
        mandar `activa` sobre su propia empresa, que es inocuo. Lo que no
        puede es desactivarla, porque se quedaría fuera en la petición
        siguiente y la única salida sería otro admin o el CLI.
        """
        if empresa_id == self.empresa_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail="No puedes dar de baja tu propia empresa."
            )

    def exigir_no_es_uno_mismo(self, objetivo_id: int) -> None:
        """Para la baja: nadie se da de baja a sí mismo. Antes era un 409; es
        un 403 porque lo que falla es el permiso, no un conflicto de estado."""
        if objetivo_id == self.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail="No puedes darte de baja a ti mismo."
            )

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

    # Un `join` más, no una consulta más: el estado de la empresa viaja en la
    # misma sentencia que ya releía usuario y rol.
    fila = (
        await db.execute(
            select(Usuario, Rol.codigo, Empresa.estado)
            .join(Rol, Rol.id == Usuario.rol_id)
            .join(Empresa, Empresa.id == Usuario.empresa_id)
            .where(Usuario.id == usuario_id)
        )
    ).first()
    if fila is None:
        raise _NO_AUTENTICADO

    usuario, rol_codigo, estado_empresa = fila
    motivo = motivo_cuenta_no_operativa(usuario.estado, estado_empresa)
    if motivo is not None:
        # El token sigue siendo criptográficamente válido, pero la cuenta ya
        # no: bloquear a alguien, o dar de baja su empresa, tiene efecto
        # inmediato y no cuando caduque el token.
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=motivo)
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
