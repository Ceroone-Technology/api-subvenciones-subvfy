"""Schemas de convocatoria.

La tabla `convocatoria` es una **caché local** de la BDNS, no la fuente de
verdad: el frontend consulta la BDNS directamente y solo nos manda la ficha
cuando necesita persistir algo que la referencie (un favorito, una alerta,
un análisis). De ahí `ConvocatoriaUpsert`: no es "crear una convocatoria",
es "guárdate esta copia de lo que acabo de leer en la BDNS".
"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.convocatoria import NIVELES_ADMINISTRACION

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí el ignore.
NivelAdministracion = Literal[NIVELES_ADMINISTRACION]  # type: ignore[valid-type]
CodigoBdns = Annotated[str, Field(min_length=1, max_length=30)]


class ConvocatoriaUpsert(BaseModel):
    """Snapshot de la ficha tal y como la devuelve la BDNS.

    Solo `codigo_bdns` y `titulo` son obligatorios: si el frontend marca un
    favorito desde el listado de resultados no tiene por qué haber cargado
    la ficha completa. Los campos que no vengan no se tocan en la caché, de
    modo que un guardado parcial nunca borra datos ya sincronizados.
    """

    codigo_bdns: CodigoBdns
    titulo: str = Field(min_length=1, max_length=500)
    nivel_administracion: NivelAdministracion | None = None
    administracion: str | None = Field(default=None, max_length=200)
    organo_convocante: str | None = Field(default=None, max_length=300)
    fecha_registro: date | None = None
    url_portal_oficial: str | None = Field(default=None, max_length=500)
    financiada_mrr: bool | None = None


class ConvocatoriaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    codigo_bdns: str
    titulo: str
    nivel_administracion: str | None
    administracion: str | None
    organo_convocante: str | None
    fecha_registro: date | None
    url_portal_oficial: str | None
    financiada_mrr: bool
    sincronizado_at: datetime
