"""Completeness of a company profile (N2). **Pure**: no database, takes already-loaded values.

Five equally weighted items, the same checklist the "Mi empresa" screen shows. `perfil_completo_pct` is the
rounded share done and `perfil_pendiente` the ones still missing, in display order.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.models.empresa import Empresa

MIN_INTERESES = 3


@dataclass(frozen=True)
class ItemPerfil:
    codigo: str
    etiqueta: str


ITEMS_PERFIL = (
    ItemPerfil("actividad", "Actividad principal (CNAE)"),
    ItemPerfil("tamano_ubicacion", "Tamaño y ubicación"),
    ItemPerfil("intereses", f"Al menos {MIN_INTERESES} intereses"),
    ItemPerfil("facturacion", "Facturación anual"),
    ItemPerfil("proyecto", "Proyecto previsto"),
)


def _lleno(texto: str | None) -> bool:
    return bool(texto and texto.strip())


def completitud(empresa: Empresa, intereses: Sequence[str]) -> tuple[int, list[ItemPerfil]]:
    """`(porcentaje 0-100, items pendientes)`."""
    hecho = {
        "actividad": empresa.actividad_codigo is not None,
        "tamano_ubicacion": empresa.tamano is not None and _lleno(empresa.provincia),
        "intereses": len(set(intereses)) >= MIN_INTERESES,
        "facturacion": empresa.facturacion_tramo is not None,
        "proyecto": _lleno(empresa.proyecto_previsto),
    }
    pendientes = [item for item in ITEMS_PERFIL if not hecho[item.codigo]]
    return round(100 * (len(ITEMS_PERFIL) - len(pendientes)) / len(ITEMS_PERFIL)), pendientes
