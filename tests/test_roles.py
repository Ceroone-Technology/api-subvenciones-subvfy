"""Catálogo de roles: solo lectura, sembrado por migración."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_listar_roles(client_admin: AsyncClient) -> None:
    respuesta = await client_admin.get("/roles")
    assert respuesta.status_code == 200
    assert [r["codigo"] for r in respuesta.json()] == ["admin", "gestor", "usuario"]


@pytest.mark.asyncio
async def test_obtener_rol(client_admin: AsyncClient) -> None:
    rol = (await client_admin.get("/roles")).json()[0]
    respuesta = await client_admin.get(f"/roles/{rol['id']}")
    assert respuesta.status_code == 200
    assert respuesta.json() == rol


@pytest.mark.asyncio
async def test_obtener_rol_inexistente(client_admin: AsyncClient) -> None:
    respuesta = await client_admin.get("/roles/999999")
    assert respuesta.status_code == 404
