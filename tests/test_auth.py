"""Autenticación: login, refresh, logout y /auth/me (Hito 2, Funcionalidad 4)."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from jose import jwt
from sqlalchemy import select

from app.config import settings
from app.core.security import TIPO_ACCESS, crear_access_token, crear_refresh_token
from app.database import AsyncSessionLocal
from app.models import Usuario
from tests.conftest import PASSWORD_TEST, Sesion, crear_sesion_en_bd


@pytest.mark.asyncio
async def test_login_devuelve_tokens_y_usuario(client: AsyncClient, gestor: Sesion) -> None:
    respuesta = await client.post(
        "/auth/login", json={"email": gestor.email, "password": PASSWORD_TEST}
    )
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["token_type"] == "bearer"
    assert cuerpo["expires_in"] == settings.access_token_expire_minutes * 60
    assert cuerpo["access_token"] and cuerpo["refresh_token"]
    # El login trae ya el usuario para que el frontend no encadene otra llamada.
    assert cuerpo["usuario"]["email"] == gestor.email
    assert "password_hash" not in cuerpo["usuario"]


@pytest.mark.asyncio
async def test_login_actualiza_ultimo_acceso(client: AsyncClient, gestor: Sesion) -> None:
    async with AsyncSessionLocal() as db:
        antes = (
            await db.execute(select(Usuario.ultimo_acceso_at).where(Usuario.id == gestor.id))
        ).scalar_one()
    assert antes is None

    await client.post("/auth/login", json={"email": gestor.email, "password": PASSWORD_TEST})

    async with AsyncSessionLocal() as db:
        despues = (
            await db.execute(select(Usuario.ultimo_acceso_at).where(Usuario.id == gestor.id))
        ).scalar_one()
    assert despues is not None


@pytest.mark.asyncio
async def test_login_password_incorrecta(client: AsyncClient, gestor: Sesion) -> None:
    respuesta = await client.post(
        "/auth/login", json={"email": gestor.email, "password": "no-es-la-buena"}
    )
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_login_email_inexistente_responde_igual_que_password_mala(
    client: AsyncClient, gestor: Sesion
) -> None:
    """Misma respuesta en ambos casos: si difirieran, el login serviría para
    averiguar qué direcciones están dadas de alta."""
    inexistente = await client.post(
        "/auth/login",
        json={"email": "no-existe@test.subvfy.example.com", "password": PASSWORD_TEST},
    )
    password_mala = await client.post(
        "/auth/login", json={"email": gestor.email, "password": "no-es-la-buena"}
    )
    assert inexistente.status_code == password_mala.status_code == 401
    assert inexistente.json() == password_mala.json()


@pytest.mark.asyncio
async def test_login_de_cuenta_bloqueada(client: AsyncClient, empresa: dict) -> None:
    bloqueado = await crear_sesion_en_bd(empresa["id"], "usuario", estado="bloqueado")
    respuesta = await client.post(
        "/auth/login", json={"email": bloqueado.email, "password": PASSWORD_TEST}
    )
    assert respuesta.status_code == 403
    assert "bloqueado" in respuesta.json()["detail"]


@pytest.mark.asyncio
async def test_me_devuelve_el_usuario_del_token(client_gestor: AsyncClient, gestor: Sesion) -> None:
    respuesta = await client_gestor.get("/auth/me")
    assert respuesta.status_code == 200
    assert respuesta.json()["id"] == gestor.id


@pytest.mark.asyncio
async def test_logout_no_invalida_el_token(client_gestor: AsyncClient) -> None:
    """Documenta la consecuencia de la estrategia stateless acordada: el
    logout es del cliente, el token sigue sirviendo hasta que caduca. Si
    algún día se añade denylist, este test debe cambiar de expectativa."""
    assert (await client_gestor.post("/auth/logout")).status_code == 204
    assert (await client_gestor.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_refresh_emite_un_access_nuevo(client: AsyncClient, gestor: Sesion) -> None:
    refresh = crear_refresh_token(gestor.id)
    respuesta = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert respuesta.status_code == 200
    assert respuesta.json()["access_token"]

    # Y el access recién emitido sirve de verdad.
    nuevo = respuesta.json()["access_token"]
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {nuevo}"})
    assert me.status_code == 200
    assert me.json()["id"] == gestor.id


@pytest.mark.asyncio
async def test_refresh_rechaza_un_access_token(client: AsyncClient, gestor: Sesion) -> None:
    """El claim `tipo` es lo que impide colar un access donde va un refresh
    (y al revés): sin él, cualquier token valdría para cualquier cosa."""
    respuesta = await client.post(
        "/auth/refresh", json={"refresh_token": crear_access_token(gestor.id)}
    )
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_endpoints_normales_rechazan_un_refresh_token(
    client: AsyncClient, gestor: Sesion
) -> None:
    refresh = crear_refresh_token(gestor.id)
    respuesta = await client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_refresh_de_usuario_dado_de_baja(client: AsyncClient, empresa: dict) -> None:
    """El refresh revalida contra la base de datos: dar de baja a alguien
    corta su sesión aunque tenga un refresh de semanas todavía vigente."""
    sesion = await crear_sesion_en_bd(empresa["id"], "usuario")
    refresh = crear_refresh_token(sesion.id)

    async with AsyncSessionLocal() as db:
        usuario = await db.get(Usuario, sesion.id)
        usuario.estado = "inactivo"
        await db.commit()

    respuesta = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_token_caducado(client: AsyncClient, gestor: Sesion) -> None:
    ahora = datetime.now(UTC)
    caducado = jwt.encode(
        {
            "sub": str(gestor.id),
            "tipo": TIPO_ACCESS,
            "iat": ahora - timedelta(hours=2),
            "exp": ahora - timedelta(hours=1),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    respuesta = await client.get("/auth/me", headers={"Authorization": f"Bearer {caducado}"})
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_token_firmado_con_otra_clave(client: AsyncClient, gestor: Sesion) -> None:
    ajeno = jwt.encode(
        {
            "sub": str(gestor.id),
            "tipo": TIPO_ACCESS,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "una-clave-que-no-es-la-nuestra",
        algorithm=settings.jwt_algorithm,
    )
    respuesta = await client.get("/auth/me", headers={"Authorization": f"Bearer {ajeno}"})
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_usuario_bloqueado_despues_de_emitir_el_token(
    client: AsyncClient, empresa: dict
) -> None:
    """El token solo lleva el id y el usuario se relee en cada petición: por
    eso bloquear una cuenta tiene efecto inmediato, sin esperar a que el
    token caduque."""
    sesion = await crear_sesion_en_bd(empresa["id"], "usuario")
    assert (await client.get("/auth/me", headers=sesion.headers)).status_code == 200

    async with AsyncSessionLocal() as db:
        usuario = await db.get(Usuario, sesion.id)
        usuario.estado = "bloqueado"
        await db.commit()

    assert (await client.get("/auth/me", headers=sesion.headers)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [
        ("get", "/auth/me"),
        ("get", "/roles"),
        ("get", "/empresas"),
        ("post", "/empresas"),
        ("get", "/usuarios"),
        ("post", "/usuarios"),
    ],
)
async def test_sin_token_responde_401(client: AsyncClient, metodo: str, ruta: str) -> None:
    respuesta = await getattr(client, metodo)(ruta, **({"json": {}} if metodo == "post" else {}))
    assert respuesta.status_code == 401
    assert respuesta.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_token_con_basura_responde_401(client: AsyncClient) -> None:
    respuesta = await client.get("/auth/me", headers={"Authorization": "Bearer esto-no-es-un-jwt"})
    assert respuesta.status_code == 401
