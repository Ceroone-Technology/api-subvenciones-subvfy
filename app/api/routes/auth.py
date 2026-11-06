"""Sesión: login, refresh, logout y usuario actual.

No hay registro público a propósito: Subvfy es B2B y las altas las hace un
admin desde `POST /empresas` y `POST /usuarios`. El primer admin de una
instalación se crea con `python -m app.cli crear-admin` (no puede salir de
la API, que exige estar autenticado para crear usuarios).

El logout es del lado del cliente — ver la nota de `app.core.security`
sobre la estrategia stateless.
"""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import DbDep
from app.config import settings
from app.core.limite_intentos import cambio_de_password
from app.core.permisos import CuentaNoOperativa, UsuarioActualDep, motivo_cuenta_no_operativa
from app.core.security import (
    TIPO_REFRESH,
    TokenInvalido,
    crear_access_token,
    crear_refresh_token,
    hashear_password,
    leer_token,
    verificar_password,
)
from app.models import Empresa, Usuario
from app.schemas.auth import (
    CambiarPasswordRequest,
    LoginRequest,
    RefreshRequest,
    SesionResponse,
    TokenResponse,
    UsuarioSesionRead,
)
from app.schemas.empresa import EmpresaSesion
from app.schemas.usuario import UsuarioRead

router = APIRouter(prefix="/auth", tags=["auth"])

# A login after this much inactivity starts a new session (see `ultimo_acceso_previo_at`).
SESION_NUEVA_TRAS = timedelta(minutes=30)

_CREDENCIALES_INVALIDAS = HTTPException(
    status.HTTP_401_UNAUTHORIZED,
    detail="Email o contraseña incorrectos.",
    headers={"WWW-Authenticate": "Bearer"},
)


# Hash contra el que se verifica cuando el email no existe, para que el login
# cueste lo mismo exista o no la cuenta. Sin esto, el `or` cortocircuitaba y
# bcrypt no se ejecutaba: 55 ms frente a 273 ms, suficiente para enumerar
# direcciones con una sola peticion (AUD-010).
#
# Se genera con `hashear_password`, no a mano: asi comparte coste con los
# hashes reales por construccion. Una constante escrita con otras rondas
# reintroduciria la diferencia que esto viene a quitar. De una contrasena
# aleatoria que nadie conoce, para que no haya ninguna entrada que la acierte.
_HASH_DE_RELLENO = hashear_password(secrets.token_urlsafe(32))


def _segundos_de_access() -> int:
    return settings.access_token_expire_minutes * 60


@router.post("/login", response_model=SesionResponse, summary="Iniciar sesión")
async def login(datos: LoginRequest, db: DbDep) -> SesionResponse:
    # El estado de la empresa viaja en la misma consulta: la baja de un cliente
    # corta el acceso de toda su gente, y la regla es la de `usuario_actual`.
    fila = (
        await db.execute(
            select(Usuario, Empresa.estado)
            .join(Empresa, Empresa.id == Usuario.empresa_id)
            .where(Usuario.email == datos.email)
        )
    ).first()

    # Mismo error para "no existe el email" que para "contraseña incorrecta":
    # distinguirlos convertiría el login en un comprobador de qué direcciones
    # están dadas de alta. Y mismo **tiempo**, que es lo que fallaba: se
    # verifica siempre contra un hash —el del usuario, o el de relleno si no
    # existe—, así que bcrypt se ejecuta en las dos ramas. La llamada va fuera
    # de cualquier `or` a propósito: detrás de uno, un cortocircuito la
    # saltaría y volveríamos al punto de partida.
    hash_a_verificar = fila[0].password_hash if fila is not None else _HASH_DE_RELLENO
    password_correcta = verificar_password(datos.password, hash_a_verificar)
    if fila is None or not password_correcta:
        raise _CREDENCIALES_INVALIDAS

    # Usuario y empresa se miran después de pagar el coste de bcrypt:
    # comprobarlos antes devolvería el 403 sin verificar nada y sería otro
    # canal por el que distinguir una cuenta existente de una que no lo es.
    usuario, estado_empresa = fila
    motivo = motivo_cuenta_no_operativa(usuario.estado, estado_empresa)
    if motivo is not None:
        raise CuentaNoOperativa(usuario.estado, estado_empresa)

    # A new session (not a second login minutes later) pushes the last access into "previous access".
    if usuario.ultimo_acceso_at is not None and datetime.now(UTC) - usuario.ultimo_acceso_at >= SESION_NUEVA_TRAS:
        usuario.ultimo_acceso_previo_at = usuario.ultimo_acceso_at
    usuario.ultimo_acceso_at = func.now()
    await db.commit()
    await db.refresh(usuario)

    return SesionResponse(
        access_token=crear_access_token(usuario.id),
        refresh_token=crear_refresh_token(usuario.id),
        expires_in=_segundos_de_access(),
        usuario=UsuarioRead.model_validate(usuario),
    )


@router.post("/refresh", response_model=TokenResponse, summary="Renovar el access token")
async def refresh(datos: RefreshRequest, db: DbDep) -> TokenResponse:
    try:
        usuario_id = leer_token(datos.refresh_token, TIPO_REFRESH)
    except TokenInvalido as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido o caducado.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Se revalida contra la base de datos: ni un usuario dado de baja ni la
    # gente de una empresa dada de baja puede seguir renovando su sesión con
    # un refresh emitido antes. Aquí el motivo no se detalla: el 401 genérico
    # ya cubre a la vez el token inservible y la cuenta inutilizable.
    fila = (
        await db.execute(
            select(Usuario.id, Usuario.estado, Empresa.estado)
            .join(Empresa, Empresa.id == Usuario.empresa_id)
            .where(Usuario.id == usuario_id)
        )
    ).first()
    if fila is None or motivo_cuenta_no_operativa(fila[1], fila[2]) is not None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido o caducado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Solo se devuelve un access nuevo. Emitir también un refresh nuevo no
    # aportaría nada mientras el anterior siga siendo válido (no hay dónde
    # revocarlo); cuando el refresh caduque, toca volver a hacer login.
    return TokenResponse(access_token=crear_access_token(fila[0]), expires_in=_segundos_de_access())


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Cerrar sesión")
async def logout(actual: UsuarioActualDep) -> None:
    """Cierre de sesión del lado del cliente: el frontend descarta los tokens.

    Existe para que el frontend tenga un punto único al que llamar (y para
    poder registrar el evento el día que haga falta auditoría de accesos),
    pero **no invalida el token en el servidor**: con la estrategia
    stateless acordada, el access sigue siendo válido hasta que caduca.
    """
    return None


@router.get("/me", response_model=UsuarioSesionRead, summary="Usuario de la sesión actual")
async def usuario_de_la_sesion(actual: UsuarioActualDep, db: DbDep) -> UsuarioSesionRead:
    # Una consulta más (por PK) a cambio de que el front sepa en el arranque si hay que pedir la actividad.
    empresa = await db.get(Empresa, actual.usuario.empresa_id)
    assert empresa is not None  # FK NOT NULL: un usuario siempre tiene empresa
    return UsuarioSesionRead.model_validate(
        {**UsuarioRead.model_validate(actual.usuario).model_dump(), "empresa": EmpresaSesion.model_validate(empresa)}
    )


@router.post(
    "/cambiar-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cambiar mi contraseña (pide la actual)",
    responses={
        400: {"description": "La contraseña actual no es correcta, o la nueva es igual a la actual."},
        429: {"description": "Demasiados intentos fallidos seguidos."},
    },
)
async def cambiar_password(datos: CambiarPasswordRequest, db: DbDep, actual: UsuarioActualDep) -> None:
    """Un 400 y no un 401 cuando la actual es incorrecta: el interceptor del frontend renueva el token ante un 401,
    y aquí el token es válido; lo que falla es el dato. La nueva sigue la política de las claves nuevas (mínimo 12
    caracteres, máximo 72 bytes)."""
    if cambio_de_password.bloqueado(actual.id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, detail="Demasiados intentos fallidos. Inténtalo en unos minutos."
        )
    # bcrypt se paga siempre, acierte o no, igual que en el login.
    if not verificar_password(datos.password_actual, actual.usuario.password_hash):
        cambio_de_password.registrar_fallo(actual.id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="La contraseña actual no es correcta.")
    if datos.password_nueva == datos.password_actual:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="La contraseña nueva debe ser distinta de la actual.")
    cambio_de_password.olvidar(actual.id)
    actual.usuario.password_hash = hashear_password(datos.password_nueva)
    actual.usuario.updated_by = actual.id
    await db.commit()
