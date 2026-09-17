"""CRUD de empresas (Hito 2, Funcionalidad 3).

Las reglas de quién puede hacer qué se prueban en test_permisos.py; aquí se
opera siempre como admin para centrarse en el comportamiento del CRUD.
"""

import pytest
from httpx import AsyncClient

from tests.conftest import nif_de_prueba


@pytest.mark.asyncio
async def test_crear_empresa(client_admin: AsyncClient, nif_unico: str) -> None:
    respuesta = await client_admin.post(
        "/empresas",
        json={
            "razon_social": "Bigtoone Test SL",
            "nif": nif_unico.lower(),  # se normaliza a mayúsculas
            "sector": "Tecnología",
            "tamano": "pequena",
            "ccaa": "Comunidad de Madrid",
        },
    )
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["id"] > 0
    assert cuerpo["nif"] == nif_unico
    assert cuerpo["estado"] == "activa"
    assert cuerpo["created_at"]


@pytest.mark.asyncio
async def test_crear_empresa_nif_duplicado(client_admin: AsyncClient, empresa: dict) -> None:
    respuesta = await client_admin.post(
        "/empresas", json={"razon_social": "Otra SL", "nif": empresa["nif"]}
    )
    assert respuesta.status_code == 409


@pytest.mark.asyncio
async def test_crear_empresa_tamano_invalido(client_admin: AsyncClient, nif_unico: str) -> None:
    respuesta = await client_admin.post(
        "/empresas", json={"razon_social": "X SL", "nif": nif_unico, "tamano": "enorme"}
    )
    assert respuesta.status_code == 422


@pytest.mark.asyncio
async def test_obtener_empresa(client_admin: AsyncClient, empresa: dict) -> None:
    respuesta = await client_admin.get(f"/empresas/{empresa['id']}")
    assert respuesta.status_code == 200
    assert respuesta.json()["nif"] == empresa["nif"]


@pytest.mark.asyncio
async def test_obtener_empresa_inexistente(client_admin: AsyncClient) -> None:
    assert (await client_admin.get("/empresas/999999")).status_code == 404


@pytest.mark.asyncio
async def test_listar_empresas_filtra_por_nif(client_admin: AsyncClient, empresa: dict) -> None:
    respuesta = await client_admin.get("/empresas", params={"q": empresa["nif"]})
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert cuerpo["page"] == 1
    assert [e["id"] for e in cuerpo["items"]] == [empresa["id"]]


@pytest.mark.asyncio
async def test_listar_empresas_pagina_vacia(client_admin: AsyncClient, empresa: dict) -> None:
    respuesta = await client_admin.get(
        "/empresas", params={"q": empresa["nif"], "page": 2, "size": 20}
    )
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1  # el total es del filtro, no de la página
    assert cuerpo["items"] == []


@pytest.mark.asyncio
async def test_actualizar_empresa(client_admin: AsyncClient, empresa: dict) -> None:
    respuesta = await client_admin.patch(
        f"/empresas/{empresa['id']}", json={"sector": "Consultoría", "tamano": "mediana"}
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["sector"] == "Consultoría"
    assert cuerpo["tamano"] == "mediana"
    assert cuerpo["razon_social"] == empresa["razon_social"]  # no tocado por el PATCH


@pytest.mark.asyncio
async def test_actualizar_empresa_nif_de_otra(client_admin: AsyncClient, empresa: dict) -> None:
    otra = (
        await client_admin.post(
            "/empresas", json={"razon_social": "Otra SL", "nif": nif_de_prueba()}
        )
    ).json()
    respuesta = await client_admin.patch(f"/empresas/{otra['id']}", json={"nif": empresa["nif"]})
    assert respuesta.status_code == 409


@pytest.mark.asyncio
async def test_baja_logica_de_empresa(client_admin: AsyncClient, empresa: dict) -> None:
    assert (await client_admin.delete(f"/empresas/{empresa['id']}")).status_code == 204

    # Baja lógica: la fila sigue existiendo, con estado "inactiva".
    respuesta = await client_admin.get(f"/empresas/{empresa['id']}")
    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "inactiva"


@pytest.mark.asyncio
async def test_crear_empresa_registra_autoria(client_admin: AsyncClient, admin, nif_unico: str) -> None:
    """created_by/updated_by dejan de ser NULL ahora que hay usuario autenticado."""
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import Empresa

    creada = (
        await client_admin.post("/empresas", json={"razon_social": "Auditada SL", "nif": nif_unico})
    ).json()

    async with AsyncSessionLocal() as db:
        fila = (
            await db.execute(
                select(Empresa.created_by, Empresa.updated_by).where(Empresa.id == creada["id"])
            )
        ).one()
    assert fila.created_by == admin.id
    assert fila.updated_by == admin.id
