"""Role-testing accounts for local development (`python -m app.cli crear-cuentas-prueba`).

Creates one gestor and one usuario inside the company of a given user, with a random password generated at run
time and returned to the caller (the CLI prints it once; it is never logged nor stored).

- Same guard as `seed-prueba`: refuses to run unless `ENVIRONMENT` is `development`.
- Idempotent: existing accounts are reused; a rerun resets the password and reactivates them.
- The usuario is made responsable of 2 cards of the company's board so "moves only assigned cards" can be tried.

The domain is NOT `@test.subvfy.example.com` on purpose: the test-suite teardown deletes every user with that
domain. `.test` is rejected by `EmailStr`.
"""

import secrets
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import hashear_password
from app.models import Empresa, Rol, Seguimiento, Usuario

DOMINIO = "prueba.subvfy.example.com"
EMAIL_GESTOR = f"gestor.prueba@{DOMINIO}"
EMAIL_USUARIO = f"usuario.prueba@{DOMINIO}"
TARJETAS_ASIGNADAS = 2
LONGITUD_PASSWORD = 20


class CuentasRechazadas(Exception):
    """The command must not run (production, unknown user or missing role)."""


@dataclass(frozen=True)
class Cuenta:
    email: str
    rol: str
    password: str
    creada: bool


@dataclass(frozen=True)
class Resultado:
    empresa: str
    cuentas: list[Cuenta]
    tarjetas_asignadas: int


def exigir_entorno_de_desarrollo() -> None:
    if settings.environment != "development":
        raise CuentasRechazadas(
            "crear-cuentas-prueba solo corre con ENVIRONMENT=development "
            f"(ahora: {settings.environment}). No se ha tocado nada."
        )


def _password_aleatoria() -> str:
    return secrets.token_urlsafe(LONGITUD_PASSWORD)[:LONGITUD_PASSWORD]


async def _cuenta(
    db: AsyncSession, empresa: Empresa, rol: Rol, email: str, nombre: str, autor: int
) -> tuple[Usuario, Cuenta]:
    password = _password_aleatoria()
    usuario = await db.scalar(select(Usuario).where(Usuario.email == email))
    creada = usuario is None
    if usuario is None:
        usuario = Usuario(
            empresa_id=empresa.id, rol_id=rol.id, nombre=nombre, apellidos="De Prueba", email=email,
            password_hash=hashear_password(password), estado="activo", created_by=autor, updated_by=autor,
        )  # fmt: skip
        db.add(usuario)
    else:
        if usuario.empresa_id != empresa.id:
            raise CuentasRechazadas(f"{email} ya existe en otra empresa. No se ha tocado nada.")
        usuario.rol_id = rol.id
        usuario.estado = "activo"
        usuario.password_hash = hashear_password(password)
        usuario.updated_by = autor
    await db.flush()
    return usuario, Cuenta(email=email, rol=rol.codigo, password=password, creada=creada)


async def _asignar_tarjetas(db: AsyncSession, usuario: Usuario, autor: int) -> int:
    """Makes `usuario` responsable of 2 board cards (idempotent: tops up to 2, preferring `preparando`)."""
    ya = list(
        await db.scalars(
            select(Seguimiento.id).where(
                Seguimiento.empresa_id == usuario.empresa_id, Seguimiento.responsable_usuario_id == usuario.id
            )
        )
    )
    faltan = TARJETAS_ASIGNADAS - len(ya)
    if faltan <= 0:
        return len(ya)
    candidatas = list(
        await db.scalars(
            select(Seguimiento.id)
            .where(Seguimiento.empresa_id == usuario.empresa_id, Seguimiento.responsable_usuario_id != usuario.id)
            .order_by((Seguimiento.estado != "preparando"), Seguimiento.id)
            .limit(faltan)
        )
    )
    if candidatas:
        await db.execute(
            update(Seguimiento)
            .where(Seguimiento.id.in_(candidatas))
            .values(responsable_usuario_id=usuario.id, updated_by=autor)
            .execution_options(synchronize_session=False)
        )
    return len(ya) + len(candidatas)


async def crear_cuentas(db: AsyncSession, email_referencia: str) -> Resultado:
    """Creates/resets the two accounts in the company of `email_referencia`. Commits once at the end."""
    exigir_entorno_de_desarrollo()
    referencia = await db.scalar(select(Usuario).where(Usuario.email == email_referencia))
    if referencia is None:
        raise CuentasRechazadas(f"No existe ningún usuario con el email {email_referencia}. No se ha tocado nada.")
    empresa = await db.get(Empresa, referencia.empresa_id)
    assert empresa is not None
    roles = {r.codigo: r for r in await db.scalars(select(Rol).where(Rol.codigo.in_(("gestor", "usuario"))))}
    if len(roles) != 2:
        raise CuentasRechazadas("Faltan los roles 'gestor' y 'usuario'. ¿Migraciones aplicadas?")

    _, cuenta_gestor = await _cuenta(db, empresa, roles["gestor"], EMAIL_GESTOR, "Gestor", referencia.id)
    usuario, cuenta_usuario = await _cuenta(db, empresa, roles["usuario"], EMAIL_USUARIO, "Usuario", referencia.id)
    asignadas = await _asignar_tarjetas(db, usuario, referencia.id)
    await db.commit()
    cuentas = [cuenta_gestor, cuenta_usuario]
    return Resultado(empresa=empresa.razon_social, cuentas=cuentas, tarjetas_asignadas=asignadas)
