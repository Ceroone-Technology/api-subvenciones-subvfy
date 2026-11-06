"""Exportación de convocatorias a CSV/XLSX (`bdns-local-sync`, PR9).

Sale de la tabla local con el mismo filtro que el buscador; nunca llama a la BDNS.
El resultado se construye en memoria: el tope de filas lo mantiene en unos pocos MB.
"""

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import busqueda_convocatorias as busqueda
from app.services.busqueda_convocatorias import FiltrosBusqueda

MAX_FILAS = 5000
FORMATOS = ("csv", "xlsx")
FormatoExportacion = Literal[FORMATOS]  # type: ignore[valid-type]

MIME_CSV = "text/csv; charset=utf-8"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

COLUMNAS = (
    "codigo_bdns", "titulo", "estado", "nivel_administracion", "administracion", "organo_convocante",
    "fecha_registro", "fecha_inicio_solicitud", "fecha_fin_solicitud", "plazo_permanente",
    "presupuesto_total", "financiada_mrr", "tipo_convocatoria", "finalidad", "url_portal_oficial",
)  # fmt: skip

_PREFIJOS_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _celda(valor: Any) -> Any:
    """Texto que Excel/Sheets interpretaría como fórmula se neutraliza con un apóstrofo (CSV injection)."""
    if isinstance(valor, bool):
        return "sí" if valor else "no"
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, float | int):
        return valor
    if valor is None:
        return ""
    if isinstance(valor, date):
        return valor.isoformat()
    texto = str(valor)
    return "'" + texto if texto.startswith(_PREFIJOS_FORMULA) else texto


async def filas(db: AsyncSession, filtros: FiltrosBusqueda, orden: str) -> list[list[Any]]:
    _, resultado = await busqueda.buscar(db, filtros, orden, offset=0, limit=MAX_FILAS)
    return [[_celda(f.get(col)) for col in COLUMNAS] for f in resultado]


def a_csv(datos: list[list[Any]]) -> bytes:
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow(COLUMNAS)
    escritor.writerows(datos)
    return salida.getvalue().encode("utf-8-sig")  # BOM: Excel abre bien las tildes


def a_xlsx(datos: list[list[Any]]) -> bytes:
    libro = Workbook(write_only=True)
    hoja = libro.create_sheet("Convocatorias")
    hoja.append(list(COLUMNAS))
    for fila in datos:
        hoja.append(fila)
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()
