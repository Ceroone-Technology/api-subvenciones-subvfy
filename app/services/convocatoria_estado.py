"""Estado de una convocatoria, calculado en SQL (`bdns-local-sync`, D4).

Nunca se guarda: un valor almacenado caducaría cada día. Se compara con el día
de hoy **en Europe/Madrid** (Postgres lleva la base de zonas horarias), y los
días de inicio y de fin son inclusivos. La bandera `abierto` de la BDNS se
ignora cuando hay fechas.
"""

from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import Date, DateTime, case, cast, func, literal
from sqlalchemy.sql.elements import ColumnElement

from app.models.convocatoria import Convocatoria

ZONA_ESTADO = "Europe/Madrid"
ABIERTA, PENDIENTE, CERRADA = "Abierta", "Pendiente", "Cerrada"
ABIERTA_SIN_PLAZO, PLAZO_NO_ESPECIFICADO = "Abierta sin plazo", "Plazo no especificado"
ESTADOS_CONVOCATORIA = (ABIERTA, PENDIENTE, CERRADA, ABIERTA_SIN_PLAZO, PLAZO_NO_ESPECIFICADO)
# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí el ignore.
EstadoConvocatoria = Literal[ESTADOS_CONVOCATORIA]  # type: ignore[valid-type]


def hoy_madrid(ahora: datetime | None = None) -> ColumnElement[date]:
    """Fecha de hoy en Madrid; `ahora` (con zona horaria) solo se inyecta en los tests."""
    instante: Any = func.now() if ahora is None else literal(ahora, DateTime(timezone=True))
    return cast(func.timezone(ZONA_ESTADO, instante), Date)


def estado_expr(hoy: date | None = None) -> ColumnElement[str]:
    """Expresión SQL del estado. `hoy` fija el día (tests y el sync); sin él, el de Madrid según la base."""
    c = Convocatoria
    dia: Any = hoy_madrid() if hoy is None else literal(hoy, Date)
    return case(
        (c.fecha_inicio_solicitud > dia, PENDIENTE),
        (c.fecha_fin_solicitud < dia, CERRADA),
        (c.fecha_fin_solicitud.is_not(None), ABIERTA),
        # Sin fin: solo hay plazo abierto si el texto de la BDNS dice que lo es.
        (c.fecha_inicio_solicitud.is_not(None) & c.plazo_permanente, ABIERTA),
        (c.plazo_permanente, ABIERTA_SIN_PLAZO),
        else_=PLAZO_NO_ESPECIFICADO,
    )
