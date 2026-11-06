"""Role-testing accounts for local development (`python -m app.cli crear-cuentas-prueba`).

Creates a SEPARATE client company, "Cliente de Prueba S.L." (never Grupo Bigtoone), so an admin can view a client as a
client. It gets a complete profile, one gestor and one usuario, and test data: favourites, 2 alerts, a follow-up board
whose cards are spread among both accounts, and the usuario responsible for 2 cards. Management requests (N14) are
added when that feature exists.

- Random passwords generated at run time and returned to the caller (the CLI prints them once; never logged/stored).
- Same guard as `seed-prueba`: refuses to run unless `ENVIRONMENT` is `development`.
- Idempotent: existing accounts are reused (a rerun resets the passwords and reactivates them); the data is only
  seeded once. The reference user (an existing account) only signs the audit columns.

The domain is NOT `@test.subvfy.example.com` on purpose: the test-suite teardown deletes every user with that
domain. `.test` is rejected by `EmailStr`.
"""

import secrets
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import hashear_password
from app.models import Empresa, Rol, Seguimiento, Usuario
from app.services import seed_prueba
from app.services.convocatoria_estado import hoy_madrid

DOMINIO = "prueba.subvfy.example.com"
EMAIL_GESTOR = f"gestor.prueba@{DOMINIO}"
EMAIL_USUARIO = f"usuario.prueba@{DOMINIO}"
RAZON_SOCIAL = "Cliente de Prueba S.L."
NIF = "PRUEBA-CLIENTE"
ACTIVIDAD = "C"
CCAA = "Comunidad de Madrid"
TARJETAS_ASIGNADAS = 2
ALERTAS_DE_PRUEBA = 2
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


async def _asignar_tarjetas(db: AsyncSession, usuario: Usuario, autor: int, gestor_id: int | None = None) -> int:
    """Makes `usuario` responsable of exactly 2 board cards (idempotent: tops up preferring `preparando`)."""
    ya = list(
        await db.scalars(
            select(Seguimiento.id).where(
                Seguimiento.empresa_id == usuario.empresa_id, Seguimiento.responsable_usuario_id == usuario.id
            ).order_by(Seguimiento.id)
        )
    )
    faltan = TARJETAS_ASIGNADAS - len(ya)
    if faltan < 0 and gestor_id is not None:
        sobran = ya[TARJETAS_ASIGNADAS:]
        await db.execute(
            update(Seguimiento)
            .where(Seguimiento.id.in_(sobran))
            .values(responsable_usuario_id=gestor_id, updated_by=autor)
            .execution_options(synchronize_session=False)
        )
        return TARJETAS_ASIGNADAS
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


async def _empresa(db: AsyncSession, autor: int) -> Empresa:
    empresa = await db.scalar(select(Empresa).where(Empresa.nif == NIF))
    if empresa is None:
        empresa = Empresa(
            razon_social=RAZON_SOCIAL, nif=NIF, estado="activa", actividad_codigo=ACTIVIDAD, ccaa=CCAA,
            descripcion="Empresa ficticia para probar la plataforma como cliente.", created_by=autor, updated_by=autor,
        )  # fmt: skip
        db.add(empresa)
    else:
        empresa.estado = "activa"
    await db.flush()
    return empresa


async def crear_cuentas(db: AsyncSession, email_referencia: str) -> Resultado:
    """Creates/resets the client company and its two accounts and seeds its data. Commits once at the end."""
    exigir_entorno_de_desarrollo()
    referencia = await db.scalar(select(Usuario).where(Usuario.email == email_referencia))
    if referencia is None:
        raise CuentasRechazadas(f"No existe ningún usuario con el email {email_referencia}. No se ha tocado nada.")
    roles = {r.codigo: r for r in await db.scalars(select(Rol).where(Rol.codigo.in_(("gestor", "usuario"))))}
    if len(roles) != 2:
        raise CuentasRechazadas("Faltan los roles 'gestor' y 'usuario'. ¿Migraciones aplicadas?")

    empresa = await _empresa(db, referencia.id)
    gestor, cuenta_gestor = await _cuenta(db, empresa, roles["gestor"], EMAIL_GESTOR, "Gestor", referencia.id)
    usuario, cuenta_usuario = await _cuenta(db, empresa, roles["usuario"], EMAIL_USUARIO, "Usuario", referencia.id)

    hoy: date = await db.scalar(select(hoy_madrid())) or date.today()
    pool = await seed_prueba._convocatorias_abiertas(db, hoy)
    resumen = seed_prueba.Resumen()
    await seed_prueba._perfil_empresa(db, empresa, gestor.id, resumen)
    await seed_prueba._favoritos(db, gestor, pool, hoy, resumen)
    await seed_prueba._alertas(db, gestor, pool, resumen, alertas=seed_prueba.ALERTAS[:ALERTAS_DE_PRUEBA])
    await seed_prueba._tablero(db, gestor, pool, hoy, resumen)
    asignadas = await _asignar_tarjetas(db, usuario, referencia.id, gestor.id)
    await db.commit()
    cuentas = [cuenta_gestor, cuenta_usuario]
    return Resultado(empresa=empresa.razon_social, cuentas=cuentas, tarjetas_asignadas=asignadas)
