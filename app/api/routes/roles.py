"""Catálogo de roles — solo lectura.

Los roles válidos están fijados por el CHECK `ck_rol_codigo` y sembrados
por migración: crearlos o borrarlos desde la API rompería el CHECK o
dejaría usuarios huérfanos. El frontend solo necesita listarlos para el
selector de alta de usuario.

Lectura para cualquier usuario autenticado: el catalogo no contiene datos
de ningun cliente, asi que no hace falta restringirlo por rol.
"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import DbDep, UsuarioActualDep
from app.models import Rol
from app.schemas.rol import RolRead

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("", response_model=list[RolRead], summary="Listar roles")
async def listar_roles(db: DbDep, actual: UsuarioActualDep) -> list[Rol]:
    # Sin paginar a propósito: son tres filas y no crecen.
    result = await db.execute(select(Rol).order_by(Rol.id))
    return list(result.scalars().all())


@router.get("/{rol_id}", response_model=RolRead, summary="Obtener un rol")
async def obtener_rol(rol_id: int, db: DbDep, actual: UsuarioActualDep) -> Rol:
    rol = await db.get(Rol, rol_id)
    if rol is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Rol no encontrado.")
    return rol
