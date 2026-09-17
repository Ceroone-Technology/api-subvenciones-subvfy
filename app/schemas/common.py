"""Piezas de schema reutilizables por todos los recursos."""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Pagina(BaseModel, Generic[T]):
    """Respuesta estándar de los listados. `total` es el número de filas que
    cumplen el filtro, no las de la página — el frontend lo necesita para
    pintar el paginador."""

    items: list[T]
    total: int
    page: int
    size: int
