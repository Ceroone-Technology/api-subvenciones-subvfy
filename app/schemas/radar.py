"""Schemas of the radar (N3). Field names match the frontend contract `OportunidadRadar` in snake_case."""

from pydantic import BaseModel, Field

from app.schemas.convocatoria import TemaConvocatoria


class OportunidadRadar(BaseModel):
    codigo_bdns: str
    titulo: str
    organo: str | None
    tema: TemaConvocatoria
    gestiona_subvfy: bool
    afinidad: int = Field(ge=0, le=100)
    # Why it fits / what to check: only true statements about the data the score was computed from.
    motivos: list[str]
    dudas: list[str]
    # Days to the close; `None` when the call has no deadline.
    dias_restantes: int | None
    # Budget of the whole call (BDNS `presupuesto_total`), never a per-project cap.
    presupuesto_total: float | None
    # Community id of the map (`and`, `ara`, ...), `ES` for state-wide, `None` when unknown.
    zona: str | None


class ResultadoRadar(BaseModel):
    """Best affinity first. `total` counts every call that reaches `min`, not only the `limit` returned."""

    items: list[OportunidadRadar]
    total: int
