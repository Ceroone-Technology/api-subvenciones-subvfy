"""Rol es un catálogo cerrado: los códigos válidos están fijados por el
CHECK `ck_rol_codigo` y sembrados por migración, así que la API solo los
expone en lectura (no hay create/update/delete)."""

from pydantic import BaseModel, ConfigDict


class RolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    codigo: str
    nombre: str
