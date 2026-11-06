"""Exportación de convocatorias a CSV/XLSX (`bdns-local-sync`, PR9).

Sale de la tabla local con el mismo filtro que el buscador; nunca llama a la BDNS.
El resultado se construye en memoria: el tope de filas lo mantiene en unos pocos MB.

Límites contra el vaciado de la fuente de datos: tope de filas por fichero (`EXPORT_MAX_FILAS`) y de exportaciones
por usuario y día (`EXPORT_MAX_DIARIAS_USUARIO`, contadas desde `exportacion_log`, así que valen con varios procesos).
El XLSX lleva la marca de Subvfy (cabecera #42429A, filtros, congelado, fechas e importes reales, enlaces).
"""

import csv
import dataclasses
import io
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from openpyxl import Workbook
from openpyxl.cell import Cell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ExportacionLog
from app.services import busqueda_convocatorias as busqueda
from app.services.busqueda_convocatorias import FiltrosBusqueda
from app.services.convocatoria_estado import hoy_madrid

MAX_FILAS = settings.export_max_filas
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


class ExportacionesAgotadas(Exception):
    """The user already made the daily maximum of exports."""

    def __init__(self, maximo: int) -> None:
        super().__init__(maximo)
        self.maximo = maximo


async def exportaciones_de_hoy(db: AsyncSession, usuario_id: int) -> int:
    """Exports of the user since midnight in Madrid, from the log table."""
    dia = cast(func.timezone("Europe/Madrid", ExportacionLog.created_at), Date)
    consulta = select(func.count()).where(ExportacionLog.usuario_id == usuario_id, dia == hoy_madrid())
    return (await db.scalar(consulta)) or 0


async def comprobar_cupo(db: AsyncSession, usuario_id: int) -> None:
    maximo = settings.export_max_diarias_usuario
    if await exportaciones_de_hoy(db, usuario_id) >= maximo:
        raise ExportacionesAgotadas(maximo)


async def registrar(
    db: AsyncSession, *, usuario_id: int, empresa_id: int, formato: str, filtros: FiltrosBusqueda, filas: int
) -> None:
    db.add(
        ExportacionLog(
            usuario_id=usuario_id, empresa_id=empresa_id, formato=formato, filas=filas,
            filtros=jsonable_encoder(dataclasses.asdict(filtros)),
            created_by=usuario_id, updated_by=usuario_id,
        )  # fmt: skip
    )
    await db.commit()


@dataclasses.dataclass
class Exportacion:
    """Rows to write, what the filter matched in total, and whether the cap cut it."""

    crudas: list[dict[str, Any]]
    total: int

    @property
    def truncada(self) -> bool:
        return self.total > len(self.crudas)

    def para_csv(self) -> list[list[Any]]:
        return [[_celda(f.get(col)) for col in COLUMNAS] for f in self.crudas]


async def consultar(db: AsyncSession, filtros: FiltrosBusqueda, orden: str) -> Exportacion:
    total, resultado = await busqueda.buscar(db, filtros, orden, offset=0, limit=MAX_FILAS)
    return Exportacion(crudas=[dict(f) for f in resultado], total=total)


async def filas(db: AsyncSession, filtros: FiltrosBusqueda, orden: str) -> list[list[Any]]:
    return (await consultar(db, filtros, orden)).para_csv()


def a_csv(datos: list[list[Any]]) -> bytes:
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow(COLUMNAS)
    escritor.writerows(datos)
    return salida.getvalue().encode("utf-8-sig")  # BOM: Excel abre bien las tildes


MARCA = "42429A"
TINTE = "EEEFF8"
# (header, key, width, kind). Order follows the PDF (code, title, body, status, period, budget), then the rest.
COLUMNAS_XLSX: tuple[tuple[str, str, int, str], ...] = (
    ("Código BDNS", "codigo_bdns", 14, "texto"),
    ("Título", "titulo", 60, "texto"),
    ("Organismo", "organismo", 34, "texto"),
    ("Estado", "estado", 16, "texto"),
    ("Inicio de solicitud", "fecha_inicio_solicitud", 14, "fecha"),
    ("Fin de solicitud", "fecha_fin_solicitud", 14, "fecha"),
    ("Presupuesto", "presupuesto_total", 18, "euros"),
    ("Nivel", "nivel_administracion", 12, "texto"),
    ("Administración", "administracion", 28, "texto"),
    ("Fecha de registro", "fecha_registro", 14, "fecha"),
    ("Plazo permanente", "plazo_permanente", 12, "booleano"),
    ("Financiada MRR", "financiada_mrr", 12, "booleano"),
    ("Tipo de convocatoria", "tipo_convocatoria", 22, "texto"),
    ("Finalidad", "finalidad", 30, "texto"),
    ("Portal oficial", "url_portal_oficial", 40, "enlace"),
)
FORMATO_FECHA = "dd/mm/yyyy"
FORMATO_EUROS = '#,##0.00 "€"'
ETIQUETAS_FILTRO = {
    "q": "Búsqueda", "nivel": "Nivel", "organo": "Órganos", "region": "Regiones", "beneficiario": "Beneficiarios",
    "estado": "Estado", "registro_desde": "Registro desde", "registro_hasta": "Registro hasta",
    "plazo_desde": "Plazo desde", "plazo_hasta": "Plazo hasta", "presupuesto_min": "Presupuesto mínimo",
    "presupuesto_max": "Presupuesto máximo", "solo_mrr": "Solo MRR", "actividad": "Actividad", "tema": "Tema",
    "gestiona": "Gestiona Subvfy",
}  # fmt: skip


def describir_filtros(filtros: FiltrosBusqueda) -> str:
    """Human line of the applied filters ("Búsqueda: energía; Estado: Abierta"); "sin filtros" when there are none."""
    partes = []
    for campo in dataclasses.fields(filtros):
        valor = getattr(filtros, campo.name)
        if valor is None or valor is False or (isinstance(valor, list | tuple) and not valor):
            continue
        if valor is True:
            texto = "sí"
        elif isinstance(valor, list | tuple):
            texto = ", ".join(str(v) for v in valor)
        else:
            texto = str(valor)
        partes.append(f"{ETIQUETAS_FILTRO.get(campo.name, campo.name)}: {texto}")
    return "; ".join(partes) if partes else "sin filtros"


def _texto_seguro(valor: Any) -> str:
    """Text for a cell: a string that spreadsheet software would read as a formula gets an apostrophe."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto.startswith(_PREFIJOS_FORMULA) else texto


def _enlace_seguro(valor: Any) -> str | None:
    """Only plain http(s) URLs become hyperlinks (never `javascript:`, `file:` or `=HYPERLINK(...)`)."""
    texto = "" if valor is None else str(valor).strip()
    if texto.lower().startswith(("http://", "https://")) and not any(c.isspace() for c in texto):
        return texto
    return None


def _escribir(celda: Cell, clave: str, tipo: str, fila: dict[str, Any]) -> None:
    if clave == "organismo":
        valor = fila.get("organo_convocante") or fila.get("administracion")
    else:
        valor = fila.get(clave)
    if tipo == "fecha" and isinstance(valor, date):
        celda.value = valor
        celda.number_format = FORMATO_FECHA
    elif tipo == "euros" and valor is not None:
        celda.value = float(valor) if isinstance(valor, Decimal) else valor
        celda.number_format = FORMATO_EUROS
    elif tipo == "booleano":
        celda.value = "sí" if valor else "no"
    elif tipo == "enlace":
        enlace = _enlace_seguro(valor)
        celda.value = enlace if enlace else _texto_seguro(valor)
        if enlace:
            celda.hyperlink = enlace  # type: ignore[assignment]
            celda.font = Font(color=MARCA, underline="single")
    else:
        celda.value = _texto_seguro(valor)
        if clave == "titulo":
            celda.alignment = Alignment(wrap_text=True, vertical="top")


def a_xlsx(
    crudas: list[dict[str, Any]],
    *,
    filtros: FiltrosBusqueda | None = None,
    total: int | None = None,
    generado: datetime | None = None,
) -> bytes:
    generado = generado or datetime.now(UTC)
    total = len(crudas) if total is None else total
    descripcion = describir_filtros(filtros or FiltrosBusqueda())
    libro = Workbook()
    hoja = libro.active
    assert hoja is not None
    hoja.title = "Convocatorias"
    ultima = get_column_letter(len(COLUMNAS_XLSX))

    titulo_hoja = hoja["A1"]
    titulo_hoja.value = "Subvfy — Convocatorias"
    titulo_hoja.font = Font(size=16, bold=True, color=MARCA)
    hoja.merge_cells(f"A1:{ultima}1")
    hoja["A2"] = f"Exportado el {generado:%d/%m/%Y}. {len(crudas)} de {total} resultados. Fuente: BDNS."
    hoja.merge_cells(f"A2:{ultima}2")
    filtros_hoja = hoja["A3"]
    filtros_hoja.value = _texto_seguro(f"Filtros: {descripcion}.")
    filtros_hoja.alignment = Alignment(wrap_text=True, vertical="top")
    hoja.merge_cells(f"A3:{ultima}3")
    hoja.row_dimensions[3].height = 30

    cabecera = 5
    relleno = PatternFill("solid", start_color=MARCA, end_color=MARCA)
    for i, (titulo, _, ancho, _) in enumerate(COLUMNAS_XLSX, start=1):
        encabezado: Any = hoja.cell(row=cabecera, column=i, value=titulo)
        encabezado.fill = relleno
        encabezado.font = Font(bold=True, color="FFFFFF")
        encabezado.alignment = Alignment(vertical="center", wrap_text=True)
        hoja.column_dimensions[get_column_letter(i)].width = ancho
    hoja.row_dimensions[cabecera].height = 24

    tinte = PatternFill("solid", start_color=TINTE, end_color=TINTE)
    for n, fila in enumerate(crudas):
        for i, (_, clave, _, tipo) in enumerate(COLUMNAS_XLSX, start=1):
            dato: Any = hoja.cell(row=cabecera + 1 + n, column=i)
            _escribir(dato, clave, tipo, fila)
            if n % 2 == 1:
                dato.fill = tinte
    hoja.freeze_panes = f"A{cabecera + 1}"
    hoja.auto_filter.ref = f"A{cabecera}:{ultima}{cabecera + max(len(crudas), 1)}"

    propiedades = libro.properties
    propiedades.title = "Subvfy — Convocatorias"
    propiedades.creator = "Subvfy"
    propiedades.subject = "Listado de convocatorias de subvenciones (BDNS)"
    propiedades.description = f"Filtros: {descripcion}"
    propiedades.keywords = "subvenciones, BDNS, Subvfy"
    propiedades.created = generado
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()
