"""Prueba de humo del modelo de datos: inserta una fila real por unas
cuantas tablas representativas (catálogo, tenant, usuario, relación con
FK circular) y confirma que los modelos SQLAlchemy y el esquema aplicado
por Alembic coinciden. No sustituye a los tests de cada endpoint (que se
añaden por Funcionalidad a partir de Hito 2, Funcionalidad 3).
"""

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Empresa, Rol, Usuario


@pytest.mark.asyncio
async def test_roles_sembrados() -> None:
    async with AsyncSessionLocal() as db:  # type: AsyncSession
        result = await db.execute(select(Rol).order_by(Rol.id))
        roles = result.scalars().all()
        assert [r.codigo for r in roles] == ["admin", "gestor", "usuario"]


@pytest.mark.asyncio
async def test_crear_empresa_y_usuario() -> None:
    async with AsyncSessionLocal() as db:  # type: AsyncSession
        rol_usuario = (await db.execute(select(Rol).where(Rol.codigo == "usuario"))).scalar_one()

        empresa = Empresa(razon_social="Empresa de Prueba SL", nif="B00000000-TEST")
        db.add(empresa)
        await db.flush()  # asigna empresa.id sin cerrar la transacción

        usuario = Usuario(
            empresa_id=empresa.id,
            rol_id=rol_usuario.id,
            nombre="Test",
            apellidos="Usuario",
            email="test.usuario@example.test",
            password_hash="hash-de-prueba",
        )
        db.add(usuario)
        await db.flush()

        # created_by/updated_by son nullable y aquí no se asignan — valida
        # que la FK circular hacia usuario.id no bloquea la inserción.
        assert usuario.id is not None
        assert usuario.created_by is None

        await db.rollback()  # no ensucia la BD entre tests
