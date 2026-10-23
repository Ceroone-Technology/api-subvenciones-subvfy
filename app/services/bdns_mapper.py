"""Traduce el detalle de la BDNS a una fila de `convocatoria` (`bdns-local-sync`).

**Funciones puras**: sin base de datos ni red. El detalle (`/convocatorias?numConv=`)
es la fuente autoritativa de cada convocatoria; el listado solo trae el resumen.

Dos cosas se resuelven fuera, contra el catálogo (`app/services/catalogos.py`):
`organo_ids` y `region_ids`. Este módulo expone los textos que hay que resolver
(`ruta_organo`, `etiquetas_region`) y recibe los ids ya resueltos, para que
entren en `contenido_hash`: si el catálogo cambia y un órgano antes sin resolver
ahora se resuelve, el hash cambia y la fila se actualiza.

Como en el upsert de alertas, **aquí no hay respaldos** (`x or y`) para campos
que luego pasan por `COALESCE`: un dato ausente sale como `None`.
"""

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models import Convocatoria
from app.services.deduplicacion_alertas import NIVEL_BDNS_A_NUESTRO

logger = logging.getLogger(__name__)

# `numeric(16, 2)`: 14 dígitos enteros.
PRESUPUESTO_MAX = Decimal("99999999999999.99")
CENTIMOS = Decimal("0.01")

# Textos de `textInicio`/`textFin` que afirman que la convocatoria está siempre
# abierta. Conservador a propósito: "Sin plazo de solicitud" (concesión directa)
# NO cuenta, porque no dice que se pueda solicitar. Se compara sin acentos ni
# mayúsculas.
_PATRON_PERMANENTE = re.compile(r"permanente|indefinid|siempre abiert|todo el ano")


class DetalleInvalido(ValueError):
    """Al detalle le falta un campo sin el que no se puede guardar la fila."""


@dataclass(frozen=True)
class RutaOrgano:
    """Los tres niveles de texto del órgano convocante (el detalle no trae su id)."""

    nivel1: str | None
    nivel2: str | None
    nivel3: str | None


def mapear_detalle(
    detalle: dict[str, Any], *, organo_ids: Sequence[int] = (), region_ids: Sequence[int] = ()
) -> dict[str, Any]:
    """Columnas de `convocatoria` (más `contenido_hash`) para el upsert.

    `organo_ids`/`region_ids` son los ids ya resueltos contra `catalogo_bdns`.
    """
    codigo = _texto(detalle.get("codigoBDNS"))
    if codigo is None:
        raise DetalleInvalido("El detalle no trae codigoBDNS.")
    titulo = _texto(detalle.get("descripcion"))
    if titulo is None:
        raise DetalleInvalido(f"El detalle {codigo} no trae descripcion.")

    organo = ruta_organo(detalle)
    fecha_fin = _fecha(detalle.get("fechaFinSolicitud"))
    fila: dict[str, Any] = {
        "codigo_bdns": _recortar(codigo, "codigo_bdns"),
        "bdns_id": _entero(detalle.get("id")),
        "titulo": _recortar(titulo, "titulo"),
        "nivel_administracion": NIVEL_BDNS_A_NUESTRO.get((organo.nivel1 or "").upper()),
        "administracion": _recortar(organo.nivel2, "administracion"),
        "organo_convocante": _recortar(organo.nivel3, "organo_convocante"),
        "fecha_registro": _fecha(detalle.get("fechaRecepcion")),
        "financiada_mrr": bool(detalle.get("mrr", False)),
        "fecha_inicio_solicitud": _fecha(detalle.get("fechaInicioSolicitud")),
        "fecha_fin_solicitud": fecha_fin,
        "abierto_bdns": detalle["abierto"] if isinstance(detalle.get("abierto"), bool) else None,
        "plazo_permanente": es_plazo_permanente(
            _texto(detalle.get("textInicio")), _texto(detalle.get("textFin")), fecha_fin
        ),
        "presupuesto_total": _presupuesto(detalle.get("presupuestoTotal")),
        "tipo_convocatoria": _recortar(_texto(detalle.get("tipoConvocatoria")), "tipo_convocatoria"),
        "finalidad": _recortar(_texto(detalle.get("descripcionFinalidad")), "finalidad"),
        "url_bases_reguladoras": _recortar(_texto(detalle.get("urlBasesReguladoras")), "url_bases_reguladoras"),
        "sede_electronica": _recortar(_texto(detalle.get("sedeElectronica")), "sede_electronica"),
        "organo_ids": list(organo_ids),
        "region_ids": list(region_ids),
        "beneficiarios": _descripciones(detalle.get("tiposBeneficiarios")),
        "sectores": _descripciones(detalle.get("sectores")),
        "detalle": _detalle_compacto(detalle),
    }
    fila["contenido_hash"] = _hash(fila)
    return fila


def ruta_organo(detalle: dict[str, Any]) -> RutaOrgano:
    organo = detalle.get("organo")
    if not isinstance(organo, dict):
        return RutaOrgano(None, None, None)
    return RutaOrgano(_texto(organo.get("nivel1")), _texto(organo.get("nivel2")), _texto(organo.get("nivel3")))


def etiquetas_region(detalle: dict[str, Any]) -> list[str]:
    """`"ES12 - PRINCIPADO DE ASTURIAS"`, ...: el prefijo es el código NUTS del catálogo."""
    return _descripciones(detalle.get("regiones"))


def es_plazo_permanente(texto_inicio: str | None, texto_fin: str | None, fecha_fin: date | None) -> bool:
    """Solo si la BDNS lo dice con palabras y no hay fecha de fin.

    Con fecha de fin la convocatoria tiene plazo, diga lo que diga el texto.
    """
    if fecha_fin is not None:
        return False
    return any(_PATRON_PERMANENTE.search(_normalizar(t)) for t in (texto_inicio, texto_fin) if t)


def _detalle_compacto(detalle: dict[str, Any]) -> dict[str, Any]:
    documentos = detalle.get("documentos")
    return {
        "documentos": [
            {
                "id": identificador,
                "nombre": _texto(doc.get("nombreFic")),
                "descripcion": _texto(doc.get("descripcion")),
                "tamano_bytes": _entero(doc.get("long")),
                "fecha_modificacion": _texto(doc.get("datMod")),
                "fecha_publicacion": _texto(doc.get("datPublicacion")),
            }
            for doc in (documentos if isinstance(documentos, list) else [])
            if isinstance(doc, dict) and (identificador := _entero(doc.get("id"))) is not None
        ],
        "instrumentos": _descripciones(detalle.get("instrumentos")),
        "fondos": _descripciones(detalle.get("fondos")),
        "objetivos": _descripciones(detalle.get("objetivos")),
        "regiones": etiquetas_region(detalle),
        "bases_reguladoras": _texto(detalle.get("descripcionBasesReguladoras")),
    }


def _hash(fila: dict[str, Any]) -> str:
    canonico = json.dumps(fila, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _descripciones(valor: Any) -> list[str]:
    if not isinstance(valor, list):
        return []
    textos = (_texto(item.get("descripcion")) for item in valor if isinstance(item, dict))
    return [texto for texto in textos if texto is not None]


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _entero(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        return None
    return int(valor)


def _fecha(valor: Any) -> date | None:
    texto = _texto(valor)
    if texto is None:
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        logger.warning("BDNS: fecha no interpretable %r.", valor)
        return None


def _presupuesto(valor: Any) -> Decimal | None:
    if isinstance(valor, bool) or not isinstance(valor, int | float | Decimal):
        return None
    try:
        importe = Decimal(str(valor)).quantize(CENTIMOS)
    except InvalidOperation:
        return None
    return importe if abs(importe) <= PRESUPUESTO_MAX else None


def _recortar(valor: str | None, columna: str) -> str | None:
    """Recorta al límite de la columna, leído del modelo para que no haya dos verdades."""
    if valor is None:
        return None
    longitud = getattr(Convocatoria.__table__.c[columna].type, "length", None)
    if longitud is not None and len(valor) > longitud:
        logger.warning("BDNS: %s recortado de %d a %d caracteres.", columna, len(valor), longitud)
        return valor[:longitud]
    return valor


def _normalizar(texto: str) -> str:
    sin_acentos = "".join(c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c))
    return sin_acentos.lower().replace("ñ", "n")
