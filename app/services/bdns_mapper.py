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
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from app.models import Convocatoria
from app.services.deduplicacion_alertas import NIVEL_BDNS_A_NUESTRO

logger = logging.getLogger(__name__)

# `numeric(16, 2)`: 14 dígitos enteros.
PRESUPUESTO_MAX = Decimal("99999999999999.99")
CENTIMOS = Decimal("0.01")

# `bdns_id` es `Integer` (32 bits con signo); fuera de rango revienta el INSERT.
ENTERO_MAX = 2**31 - 1
ENTERO_MIN = -(2**31)
# Los enteros que solo viajan dentro del JSON (id y tamaño de documento) caben en 64 bits.
ENTERO_JSON_MAX = 2**63 - 1

# Topes de listas y del JSON `detalle`: la BDNS es una fuente externa y no se
# fía su tamaño. Al recortar se avisa en el log.
MAX_ELEMENTOS_LISTA = 200
MAX_CHARS_TEXTO_JSON = 1_000
MAX_CHARS_BASES = 20_000
MAX_BYTES_DETALLE = 262_144

_ESQUEMAS_URL = ("http", "https")

# Textos de `textInicio`/`textFin` que afirman que la convocatoria está siempre
# abierta. Conservador a propósito: "Sin plazo de solicitud" (concesión directa)
# NO cuenta, porque no dice que se pueda solicitar. Se compara sin acentos ni
# mayúsculas.
_PATRON_PERMANENTE = re.compile(r"permanente|indefinid|siempre abiert|todo el ano")
# "no permanente", "no es permanente", "no abierta de forma indefinida": un "no" a
# lo sumo dos palabras antes de la expresión la niega, sin cruzar `, . ;`.
_NEGACION_PREVIA = re.compile(r"\bno\b(?:[^\w,.;]+\w+){0,2}[^\w,.;]*$")


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
        "bdns_id": _entero(detalle.get("id"), ENTERO_MIN, ENTERO_MAX),
        "titulo": _recortar(titulo, "titulo"),
        "nivel_administracion": NIVEL_BDNS_A_NUESTRO.get((organo.nivel1 or "").upper()),
        "administracion": _recortar(organo.nivel2, "administracion"),
        "organo_convocante": _recortar(organo.nivel3, "organo_convocante"),
        "fecha_registro": _fecha(detalle.get("fechaRecepcion")),
        # `None` si falta: con el COALESCE del upsert no pisa lo ya cacheado.
        "financiada_mrr": detalle["mrr"] if isinstance(detalle.get("mrr"), bool) else None,
        "fecha_inicio_solicitud": _fecha(detalle.get("fechaInicioSolicitud")),
        "fecha_fin_solicitud": fecha_fin,
        "abierto_bdns": detalle["abierto"] if isinstance(detalle.get("abierto"), bool) else None,
        "plazo_permanente": es_plazo_permanente(
            _texto(detalle.get("textInicio")), _texto(detalle.get("textFin")), fecha_fin
        ),
        "presupuesto_total": _presupuesto(detalle.get("presupuestoTotal")),
        "tipo_convocatoria": _recortar(_texto(detalle.get("tipoConvocatoria")), "tipo_convocatoria"),
        "finalidad": _recortar(_texto(detalle.get("descripcionFinalidad")), "finalidad"),
        "url_bases_reguladoras": _recortar(_url(detalle.get("urlBasesReguladoras")), "url_bases_reguladoras"),
        "sede_electronica": _recortar(_url(detalle.get("sedeElectronica")), "sede_electronica"),
        # Ordenados: el orden en que llegan no debe cambiar el hash.
        "organo_ids": sorted(organo_ids),
        "region_ids": sorted(region_ids),
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
    return any(_afirma_permanente(_normalizar(t)) for t in (texto_inicio, texto_fin) if t)


def _afirma_permanente(texto: str) -> bool:
    """Hay una expresión de permanencia que no va precedida de un "no" (hasta 2 palabras antes)."""
    return any(not _NEGACION_PREVIA.search(texto[: m.start()]) for m in _PATRON_PERMANENTE.finditer(texto))


def _detalle_compacto(detalle: dict[str, Any]) -> dict[str, Any]:
    documentos = _acotar(detalle.get("documentos"), "documentos")
    compacto = {
        "documentos": [
            {
                "id": identificador,
                "nombre": _texto_json(doc.get("nombreFic")),
                "descripcion": _texto_json(doc.get("descripcion")),
                "tamano_bytes": _entero(doc.get("long"), 0, ENTERO_JSON_MAX),
                "fecha_modificacion": _texto_json(doc.get("datMod")),
                "fecha_publicacion": _texto_json(doc.get("datPublicacion")),
            }
            for doc in documentos
            if isinstance(doc, dict) and (identificador := _entero(doc.get("id"), 0, ENTERO_JSON_MAX)) is not None
        ],
        "instrumentos": _descripciones(detalle.get("instrumentos"), MAX_CHARS_TEXTO_JSON),
        "fondos": _descripciones(detalle.get("fondos"), MAX_CHARS_TEXTO_JSON),
        "objetivos": _descripciones(detalle.get("objetivos"), MAX_CHARS_TEXTO_JSON),
        "regiones": etiquetas_region(detalle),
        "bases_reguladoras": _texto_json(detalle.get("descripcionBasesReguladoras"), MAX_CHARS_BASES),
    }
    return _ajustar_tamano(compacto)


def _ajustar_tamano(compacto: dict[str, Any]) -> dict[str, Any]:
    """Garantiza `MAX_BYTES_DETALLE`: quita documentos por el final hasta que quepa."""
    tamano = _bytes_json(compacto)
    while tamano > MAX_BYTES_DETALLE and compacto["documentos"]:
        logger.warning("BDNS: detalle recortado, %d bytes superan %d.", tamano, MAX_BYTES_DETALLE)
        compacto["documentos"] = compacto["documentos"][: len(compacto["documentos"]) // 2]
        tamano = _bytes_json(compacto)
    if tamano > MAX_BYTES_DETALLE:
        logger.warning("BDNS: detalle recortado, %d bytes superan %d.", tamano, MAX_BYTES_DETALLE)
        compacto["bases_reguladoras"] = None
    return compacto


def _bytes_json(valor: Any) -> int:
    return len(json.dumps(valor, ensure_ascii=False).encode("utf-8"))


def _acotar(valor: Any, nombre: str) -> list[Any]:
    """Lista (o vacía si no lo es) con a lo sumo `MAX_ELEMENTOS_LISTA` elementos."""
    if not isinstance(valor, list):
        return []
    if len(valor) > MAX_ELEMENTOS_LISTA:
        logger.warning("BDNS: %s recortado de %d a %d elementos.", nombre, len(valor), MAX_ELEMENTOS_LISTA)
        return valor[:MAX_ELEMENTOS_LISTA]
    return valor


def _hash(fila: dict[str, Any]) -> str:
    canonico = json.dumps(fila, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _descripciones(valor: Any, maximo_chars: int | None = None) -> list[str]:
    textos = (_texto(item.get("descripcion")) for item in _acotar(valor, "lista") if isinstance(item, dict))
    return [_recortar_chars(texto, maximo_chars) if maximo_chars else texto for texto in textos if texto is not None]


def _texto(valor: Any) -> str | None:
    """Texto sin espacios en los extremos y sin NUL (Postgres no admite `\\x00` en texto ni en JSONB)."""
    if valor is None:
        return None
    texto = str(valor).replace("\x00", "").strip()
    return texto or None


def _texto_json(valor: Any, maximo_chars: int = MAX_CHARS_TEXTO_JSON) -> str | None:
    texto = _texto(valor)
    return None if texto is None else _recortar_chars(texto, maximo_chars)


def _recortar_chars(texto: str, maximo: int) -> str:
    if len(texto) > maximo:
        logger.warning("BDNS: texto recortado de %d a %d caracteres.", len(texto), maximo)
        return texto[:maximo]
    return texto


def _url(valor: Any) -> str | None:
    """Solo `http`/`https`: otro esquema (`javascript:`, `file:`) acabaría en un enlace del front."""
    texto = _texto(valor)
    if texto is None:
        return None
    try:
        esquema = urlsplit(texto).scheme.lower()
    except ValueError:
        return None
    return texto if esquema in _ESQUEMAS_URL else None


def _entero(valor: Any, minimo: int, maximo: int) -> int | None:
    """Entero dentro de `[minimo, maximo]`; NaN, infinitos, bools y fuera de rango dan `None`."""
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        return None
    if isinstance(valor, float) and not math.isfinite(valor):
        return None
    entero = int(valor)
    return entero if minimo <= entero <= maximo else None


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
    return importe if importe.is_finite() and abs(importe) <= PRESUPUESTO_MAX else None


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
