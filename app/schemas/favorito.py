"""Schemas de favorito."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.convocatoria import ConvocatoriaRead, ConvocatoriaUpsert

# La nota es un Text en base de datos, pero un límite explícito evita que un
# copia-pega accidental de media convocatoria entre por aquí.
Nota = Field(default=None, max_length=2000)


class FavoritoCreate(BaseModel):
    """Marcar un favorito lleva la ficha de la convocatoria dentro: es lo que
    permite sembrar la caché local en el mismo paso, sin obligar al frontend
    a conocer ningún id nuestro."""

    convocatoria: ConvocatoriaUpsert
    nota: str | None = Nota


class FavoritoUpdate(BaseModel):
    nota: str | None = Nota


class FavoritoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nota: str | None
    created_at: datetime
    updated_at: datetime
    # Resuelta por join: el listado de favoritos se pinta sin volver a la BDNS.
    convocatoria: ConvocatoriaRead
