"""CNAE helpers (`empresa-actividad-ia`, PR B2). **Pure functions**: no database, no network.

A grant carries BDNS sector codes (`{"descripcion": ..., "codigo": "84.1"}`). The picker of a
company offers CNAE-2025 sections (A-V), so a code is mapped to its section through the division
(first two digits). The table mirrors the catalog `sector_actividad` (migration `c9a4f6e1d283`, CNAE-2025)
and `tests/unit/test_cnae.py` checks both agree: change them together.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# section -> (first division, last division), CNAE-2025 (INE; the BDNS `/actividades` lists these 22 sections).
RANGOS_DIVISION: dict[str, tuple[int, int]] = {
    "A": (1, 3), "B": (5, 9), "C": (10, 33), "D": (35, 35), "E": (36, 39), "F": (41, 43),
    "G": (45, 47), "H": (49, 53), "I": (55, 56), "J": (58, 60), "K": (61, 63), "L": (64, 66),
    "M": (68, 68), "N": (69, 75), "O": (77, 82), "P": (84, 84), "Q": (85, 85), "R": (86, 88),
    "S": (90, 93), "T": (94, 96), "U": (97, 98), "V": (99, 99),
}  # fmt: skip

# "84.1", "1.1", "62" (division before the dot or alone) | "8411", "620" (no separator: first two digits).
_DIVISION = re.compile(r"^(\d{1,2})(?:\.|$)|^(\d{2})\d")


def seccion_de(codigo: str | None) -> str | None:
    """Section letter of a BDNS/CNAE code, or `None` when it does not map (logged, never raised)."""
    texto = (codigo or "").strip().upper()
    if not texto:
        return None
    if texto in RANGOS_DIVISION:
        return texto
    coincidencia = _DIVISION.match(texto)
    if coincidencia is not None:
        division = int(coincidencia.group(1) or coincidencia.group(2))
        for seccion, (desde, hasta) in RANGOS_DIVISION.items():
            if desde <= division <= hasta:
                return seccion
    logger.warning("CNAE: el código %r no se asigna a ninguna sección.", texto[:20])
    return None


def secciones_de(sectores: Any) -> list[str]:
    """Sorted, unique section letters of a BDNS `sectores` list; anything malformed is skipped."""
    if not isinstance(sectores, list):
        return []
    codigos = (s.get("codigo") for s in sectores if isinstance(s, dict))
    secciones = {seccion_de(c) for c in codigos if isinstance(c, str)}
    return sorted(s for s in secciones if s is not None)
