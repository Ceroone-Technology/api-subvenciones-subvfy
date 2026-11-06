"""Schemas of the notice preferences (plan N9a). Same shape the frontend uses: a list of `{tipo, email, app}`."""

from typing import Literal

from pydantic import BaseModel

from app.models.preferencia_aviso import TIPOS_AVISO

# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí el ignore.
TipoAviso = Literal[TIPOS_AVISO]  # type: ignore[valid-type]


class PreferenciaAvisoItem(BaseModel):
    tipo: TipoAviso
    email: bool
    app: bool
