"""CRUD de usuarios (Hito 2, Funcionalidad 3).

Las reglas de quién puede hacer qué se prueban en test_permisos.py; aquí se
opera siempre como admin para centrarse en el comportamiento del CRUD.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import verificar_password
from app.database import AsyncSessionLocal
from app.models import Usuario

PASSWORD = "secreto-de-prueba"


def _payload(empresa_id: int, rol_id: int, email: str) -> dict:
    return {
        "empresa_id": empresa_id,
        "rol_id": rol_id,
        "nombre": "Ana",
        "apellidos": "García López",
        "email": email,
        "password": PASSWORD,
    }


async def _hash_en_bd(email: str) -> str:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(select(Usuario.password_hash).where(Usuario.email == email))
        ).scalar_one()


@pytest.mark.asyncio
async def test_crear_usuario_hashea_password(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    respuesta = await client_admin.post(
        "/usuarios", json=_payload(empresa["id"], rol_usuario_id, email_unico)
    )
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "activo"
    assert cuerpo["ultimo_acceso_at"] is None
    # El hash no sale nunca de la API, ni la contraseña en claro.
    assert "password_hash" not in cuerpo
    assert "password" not in cuerpo

    hash_guardado = await _hash_en_bd(email_unico)
    assert hash_guardado != PASSWORD
    assert verificar_password(PASSWORD, hash_guardado)


@pytest.mark.asyncio
async def test_crear_usuario_email_duplicado(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    datos = _payload(empresa["id"], rol_usuario_id, email_unico)
    assert (await client_admin.post("/usuarios", json=datos)).status_code == 201
    assert (await client_admin.post("/usuarios", json=datos)).status_code == 409


@pytest.mark.asyncio
async def test_crear_usuario_con_empresa_inexistente(
    client_admin: AsyncClient, rol_usuario_id: int, email_unico: str
) -> None:
    respuesta = await client_admin.post(
        "/usuarios", json=_payload(999999, rol_usuario_id, email_unico)
    )
    assert respuesta.status_code == 400
    assert "empresa" in respuesta.json()["detail"].lower()


@pytest.mark.asyncio
async def test_crear_usuario_con_rol_inexistente(
    client_admin: AsyncClient, empresa: dict, email_unico: str
) -> None:
    respuesta = await client_admin.post(
        "/usuarios", json=_payload(empresa["id"], 999999, email_unico)
    )
    assert respuesta.status_code == 400
    assert "rol" in respuesta.json()["detail"].lower()


@pytest.mark.asyncio
async def test_crear_usuario_password_corta(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    datos = _payload(empresa["id"], rol_usuario_id, email_unico) | {"password": "corta"}
    assert (await client_admin.post("/usuarios", json=datos)).status_code == 422


@pytest.mark.asyncio
async def test_crear_usuario_password_supera_limite_bcrypt(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    # bcrypt trunca a 72 bytes: se rechaza explícitamente en vez de aceptar
    # una contraseña que en realidad se guardaría recortada.
    datos = _payload(empresa["id"], rol_usuario_id, email_unico) | {"password": "a" * 73}
    assert (await client_admin.post("/usuarios", json=datos)).status_code == 422


@pytest.mark.asyncio
async def test_crear_usuario_email_invalido(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int
) -> None:
    datos = _payload(empresa["id"], rol_usuario_id, "no-es-un-email")
    assert (await client_admin.post("/usuarios", json=datos)).status_code == 422


@pytest.mark.asyncio
async def test_listar_usuarios_filtra_por_empresa(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    creado = (
        await client_admin.post(
            "/usuarios", json=_payload(empresa["id"], rol_usuario_id, email_unico)
        )
    ).json()

    respuesta = await client_admin.get("/usuarios", params={"empresa_id": empresa["id"]})
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert [u["id"] for u in cuerpo["items"]] == [creado["id"]]


@pytest.mark.asyncio
async def test_actualizar_usuario_cambia_password(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    creado = (
        await client_admin.post(
            "/usuarios", json=_payload(empresa["id"], rol_usuario_id, email_unico)
        )
    ).json()
    hash_inicial = await _hash_en_bd(email_unico)

    respuesta = await client_admin.patch(
        f"/usuarios/{creado['id']}", json={"password": "otra-password-valida", "nombre": "Ana María"}
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["nombre"] == "Ana María"

    hash_nuevo = await _hash_en_bd(email_unico)
    assert hash_nuevo != hash_inicial
    assert verificar_password("otra-password-valida", hash_nuevo)
    assert not verificar_password(PASSWORD, hash_nuevo)


@pytest.mark.asyncio
async def test_actualizar_usuario_inexistente(client_admin: AsyncClient) -> None:
    assert (await client_admin.patch("/usuarios/999999", json={"nombre": "X"})).status_code == 404


@pytest.mark.asyncio
async def test_baja_logica_de_usuario(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    creado = (
        await client_admin.post(
            "/usuarios", json=_payload(empresa["id"], rol_usuario_id, email_unico)
        )
    ).json()

    assert (await client_admin.delete(f"/usuarios/{creado['id']}")).status_code == 204

    respuesta = await client_admin.get(f"/usuarios/{creado['id']}")
    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "inactivo"


@pytest.mark.asyncio
async def test_no_puedes_darte_de_baja_a_ti_mismo(client_admin: AsyncClient, admin) -> None:
    """Sin esta guarda, el único admin de una instalación puede dejarse fuera
    con una sola llamada y ya no hay quien lo reactive por la API."""
    respuesta = await client_admin.delete(f"/usuarios/{admin.id}")
    assert respuesta.status_code == 409
