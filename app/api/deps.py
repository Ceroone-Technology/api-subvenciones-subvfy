"""Dependencias compartidas por los routers.

Alias `Annotated` para no repetir `Depends(...)` en cada firma de endpoint.
Las de autenticación y autorización viven en `app.core.permisos` (donde
está también el modelo de permisos por rol) y se reexportan aquí para que
los routers importen todas sus dependencias del mismo sitio.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permisos import AdminDep, GestorDep, UsuarioActualDep
from app.database import get_db


@dataclass(frozen=True)
class Paginacion:
    page: int
    size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def paginacion(
    page: Annotated[int, Query(ge=1, description="Número de página, empezando en 1.")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="Elementos por página.")] = 20,
) -> Paginacion:
    return Paginacion(page=page, size=size)


DbDep = Annotated[AsyncSession, Depends(get_db)]
PaginacionDep = Annotated[Paginacion, Depends(paginacion)]


__all__ = [
    "AdminDep",
    "DbDep",
    "GestorDep",
    "Paginacion",
    "PaginacionDep",
    "UsuarioActualDep",
    "paginacion",
]
