"""Schemas of the discards (N4)."""

from datetime import datetime

from pydantic import BaseModel

from app.schemas.convocatoria import CodigoBdns


class DescarteCreate(BaseModel):
    codigo_bdns: CodigoBdns


class DescarteRead(BaseModel):
    codigo_bdns: str
    descartada_at: datetime
