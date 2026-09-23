"""Fixtures compartidas de test.

`client` es un cliente HTTP async contra la app FastAPI en memoria (sin
levantar un servidor real), vía httpx.AsyncClient + ASGITransport. Desde el
Hito 2 Funcionalidad 4 casi todos los endpoints exigen autenticación, así
que hay además un cliente por rol (`client_admin`, `client_gestor`,
`client_usuario`); `client` se reserva para comprobar los 401.

Los usuarios de prueba se crean **directamente en la base de datos**, no
por la API: crear un usuario exige estar autenticado, y no habría por dónde
empezar. Es el mismo arranque en frío que resuelve `app/cli.py` en real.

Los tests de endpoints escriben en una base de datos real (los commits de
los routers no se pueden deshacer con un rollback desde fuera), así que
`limpiar_datos_de_test` borra al final de cada test las filas creadas. Se
apoya en dos convenciones que todos los tests respetan: los NIF empiezan
por `TEST-` y los emails terminan en EMAIL_DOMINIO_TEST.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.security import crear_access_token, hashear_password
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Alerta, Convocatoria, Empresa, Rol, Usuario

NIF_PREFIJO_TEST = "TEST-"
# Subdominio de example.com (RFC 2606, nunca entregable) y no un TLD
# reservado como .test: email-validator, que usa Pydantic para EmailStr,
# rechaza .test/.invalid/.localhost como direcciones no válidas.
EMAIL_DOMINIO_TEST = "@test.subvfy.example.com"
PASSWORD_TEST = "password-de-prueba"
# Las convocatorias de prueba tambien necesitan su marca: son filas de la
# cache local que los favoritos siembran al vuelo.
CODIGO_BDNS_PREFIJO_TEST = "TST"


@dataclass(frozen=True)
class Sesion:
    """Un usuario de prueba junto con su access token ya emitido."""

    id: int
    email: str
    empresa_id: int
    rol: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def nif_de_prueba() -> str:
    """NIF irrepetible: el campo es UNIQUE y los tests corren contra una base
    real que puede conservar restos de una ejecución interrumpida."""
    return f"{NIF_PREFIJO_TEST}{uuid4().hex[:8].upper()}"


def email_de_prueba(prefijo: str = "test") -> str:
    return f"{prefijo}-{uuid4().hex[:8]}{EMAIL_DOMINIO_TEST}"


def codigo_bdns_de_prueba() -> str:
    """Codigo BDNS irrepetible (la columna es UNIQUE, maximo 30 caracteres)."""
    return f"{CODIGO_BDNS_PREFIJO_TEST}{uuid4().hex[:10].upper()}"


async def crear_empresa_en_bd(razon_social: str = "Empresa de Prueba SL", nif: str | None = None) -> dict:
    async with AsyncSessionLocal() as db:
        empresa = Empresa(razon_social=razon_social, nif=nif or nif_de_prueba(), tamano="pequena")
        db.add(empresa)
        await db.commit()
        await db.refresh(empresa)
        return {
            "id": empresa.id,
            "razon_social": empresa.razon_social,
            "nif": empresa.nif,
            "estado": empresa.estado,
        }


async def crear_sesion_en_bd(
    empresa_id: int, rol_codigo: str, *, password: str = PASSWORD_TEST, estado: str = "activo"
) -> Sesion:
    async with AsyncSessionLocal() as db:
        rol_id = (await db.execute(select(Rol.id).where(Rol.codigo == rol_codigo))).scalar_one()
        usuario = Usuario(
            empresa_id=empresa_id,
            rol_id=rol_id,
            nombre=rol_codigo.capitalize(),
            apellidos="De Prueba",
            email=email_de_prueba(rol_codigo),
            password_hash=hashear_password(password),
            estado=estado,
        )
        db.add(usuario)
        await db.commit()
        await db.refresh(usuario)
        return Sesion(
            id=usuario.id,
            email=usuario.email,
            empresa_id=usuario.empresa_id,
            rol=rol_codigo,
            token=crear_access_token(usuario.id),
        )


async def crear_convocatoria_en_bd(titulo: str = "Ayudas a la digitalización", **extra) -> Convocatoria:
    """Siembra una fila de la caché local de convocatorias.

    Los favoritos la crean por la API al marcarlos, pero alertas y análisis
    la necesitan ya existente: la devuelve desasociada de la sesión, así que
    quien la use debe recargarla en la suya (o quedarse con el id).
    """
    async with AsyncSessionLocal() as db:
        convocatoria = Convocatoria(codigo_bdns=codigo_bdns_de_prueba(), titulo=titulo, **extra)
        db.add(convocatoria)
        await db.commit()
        await db.refresh(convocatoria)
        db.expunge(convocatoria)
        return convocatoria


async def crear_alerta_en_bd(usuario_id: int, **extra) -> int:
    """Devuelve el id: la alerta se recarga en la sesión donde vaya a usarse."""
    async with AsyncSessionLocal() as db:
        alerta = Alerta(
            usuario_id=usuario_id,
            nombre=extra.pop("nombre", "Alerta de prueba"),
            created_by=usuario_id,
            updated_by=usuario_id,
            **extra,
        )
        db.add(alerta)
        await db.commit()
        return alerta.id


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Cliente sin autenticar. Solo para los tests de 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(autouse=True)
async def limpiar_datos_de_test() -> AsyncGenerator[None, None]:
    yield
    async with AsyncSessionLocal() as db:
        # Las FK de auditoría apuntan a usuario.id: hay que soltarlas antes
        # de borrar, o el DELETE choca con las filas que esos usuarios firmaron.
        await db.execute(
            Empresa.__table__.update()
            .where(Empresa.nif.like(f"{NIF_PREFIJO_TEST}%"))
            .values(created_by=None, updated_by=None)
        )
        await db.execute(
            Usuario.__table__.update()
            .where(Usuario.email.like(f"%{EMAIL_DOMINIO_TEST}"))
            .values(created_by=None, updated_by=None)
        )
        await db.execute(
            Convocatoria.__table__.update()
            .where(Convocatoria.codigo_bdns.like(f"{CODIGO_BDNS_PREFIJO_TEST}%"))
            .values(created_by=None, updated_by=None)
        )
        # Las alertas no pueden esperar a la cascada del usuario: sus filtros
        # quedan a dos niveles (usuario -> alerta -> alerta_organo) y
        # Postgres comprueba su FK de auditoría created_by -> usuario antes de
        # que esa cascada llegue a borrarlos.
        usuarios_de_test = select(Usuario.id).where(Usuario.email.like(f"%{EMAIL_DOMINIO_TEST}"))
        await db.execute(delete(Alerta).where(Alerta.usuario_id.in_(usuarios_de_test)))
        # Usuarios primero (sus favoritos caen por ON DELETE CASCADE), luego
        # las convocatorias cacheadas y por ultimo las empresas.
        await db.execute(delete(Usuario).where(Usuario.email.like(f"%{EMAIL_DOMINIO_TEST}")))
        await db.execute(
            delete(Convocatoria).where(Convocatoria.codigo_bdns.like(f"{CODIGO_BDNS_PREFIJO_TEST}%"))
        )
        await db.execute(delete(Empresa).where(Empresa.nif.like(f"{NIF_PREFIJO_TEST}%")))
        await db.commit()


@pytest.fixture
def nif_unico() -> str:
    return nif_de_prueba()


@pytest.fixture
def email_unico() -> str:
    return email_de_prueba()


@pytest_asyncio.fixture
async def empresa() -> dict:
    """La empresa cliente sobre la que operan la mayoría de los tests."""
    return await crear_empresa_en_bd()


@pytest_asyncio.fixture
async def otra_empresa() -> dict:
    """Una segunda empresa, para comprobar que no hay fugas entre tenants."""
    return await crear_empresa_en_bd(razon_social="Otra Empresa SL")


@pytest_asyncio.fixture
async def admin() -> Sesion:
    """Admin en su propia empresa: así, si un test le ve datos de la empresa
    cliente, es porque el rol se los da, no porque comparta tenant."""
    empresa_admin = await crear_empresa_en_bd(razon_social="Administracion Subvfy SL")
    return await crear_sesion_en_bd(empresa_admin["id"], "admin")


@pytest_asyncio.fixture
async def gestor(empresa: dict) -> Sesion:
    return await crear_sesion_en_bd(empresa["id"], "gestor")


@pytest_asyncio.fixture
async def usuario_raso(empresa: dict) -> Sesion:
    return await crear_sesion_en_bd(empresa["id"], "usuario")


def _cliente_autenticado(sesion: Sesion) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test", headers=sesion.headers)


@pytest_asyncio.fixture
async def client_admin(admin: Sesion) -> AsyncGenerator[AsyncClient, None]:
    async with _cliente_autenticado(admin) as ac:
        yield ac


@pytest_asyncio.fixture
async def client_gestor(gestor: Sesion) -> AsyncGenerator[AsyncClient, None]:
    async with _cliente_autenticado(gestor) as ac:
        yield ac


@pytest_asyncio.fixture
async def client_usuario(usuario_raso: Sesion) -> AsyncGenerator[AsyncClient, None]:
    async with _cliente_autenticado(usuario_raso) as ac:
        yield ac


@pytest_asyncio.fixture
async def rol_usuario_id() -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Rol.id).where(Rol.codigo == "usuario"))).scalar_one()
