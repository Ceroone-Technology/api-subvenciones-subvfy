"""Catálogos en la forma de nodo de la BDNS, para que el mapper del front y los ids de las alertas sigan valiendo."""

from pydantic import BaseModel


class NodoCatalogo(BaseModel):
    """`{id, descripcion, children}`. `id` es el id BDNS (órganos, regiones) o el propio texto (beneficiarios)."""

    id: int | str
    descripcion: str
    children: list["NodoCatalogo"] = []
