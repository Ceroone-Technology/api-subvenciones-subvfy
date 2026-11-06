"""Authenticated export of the filtered result set (PR9, tasks 9.1)."""

import csv
import io
import random
import string
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.config import settings
from app.core.limite_intentos import LimiteIntentos
from app.database import AsyncSessionLocal
from app.models import ExportacionLog
from app.services import exportacion_convocatorias as exportacion
from tests.conftest import crear_convocatoria_en_bd


def marca() -> str:
    return "zz" + "".join(random.choices(string.ascii_lowercase, k=12))


async def sembrar(m: str, n: int, **extra: Any) -> None:
    for i in range(n):
        await crear_convocatoria_en_bd(f"Ayudas {m} numero{i}", **extra)


async def test_csv_exporta_todas_las_filas_del_filtro(client_usuario: AsyncClient) -> None:
    m = marca()
    await sembrar(m, 12)
    await crear_convocatoria_en_bd("Otra cosa distinta")
    r = await client_usuario.get("/convocatorias/export", params={"formato": "csv", "q": m})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="convocatorias-' in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.csv"')
    filas = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert len(filas) == 13  # cabecera + 12
    assert filas[0][0] == "codigo_bdns"


async def test_xlsx_exporta_las_mismas_filas(client_usuario: AsyncClient) -> None:
    m = marca()
    await sembrar(m, 12)
    r = await client_usuario.get("/convocatorias/export", params={"formato": "xlsx", "q": m})
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.headers["content-disposition"].endswith('.xlsx"')
    hoja = load_workbook(io.BytesIO(r.content)).active
    assert hoja is not None
    assert hoja.max_row == 5 + 12  # title, date, filters, blank, header + 12 rows
    assert hoja.cell(row=5, column=1).value == "Código BDNS"


async def test_el_tope_de_filas_se_respeta(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    m = marca()
    await sembrar(m, 5)
    assert exportacion.MAX_FILAS == 5000
    monkeypatch.setattr(exportacion, "MAX_FILAS", 3)
    r = await client_usuario.get("/convocatorias/export", params={"formato": "csv", "q": m})
    assert len(list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))) == 4


async def test_celdas_que_parecen_formula_se_neutralizan(client_usuario: AsyncClient) -> None:
    m = marca()
    await crear_convocatoria_en_bd(f"=HYPERLINK(1) {m}")
    r = await client_usuario.get("/convocatorias/export", params={"formato": "csv", "q": m})
    filas = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert filas[1][1].startswith("'=")


async def test_formato_invalido_es_422_y_anonimo_401(client_usuario: AsyncClient, client: AsyncClient) -> None:
    assert (await client_usuario.get("/convocatorias/export", params={"formato": "pdf"})).status_code == 422
    assert (await client_usuario.get("/convocatorias/export")).status_code == 422
    assert (await client.get("/convocatorias/export", params={"formato": "csv"})).status_code == 401


def test_content_disposition_se_expone_por_cors() -> None:
    from app.main import app

    cors = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert "Content-Disposition" in cors.kwargs["expose_headers"]


# --- Branded XLSX -----------------------------------------------------------------------------


async def _xlsx(cliente: AsyncClient, **params: Any):  # noqa: ANN202
    r = await cliente.get("/convocatorias/export", params={"formato": "xlsx", **params})
    assert r.status_code == 200, r.text
    return load_workbook(io.BytesIO(r.content)), r


async def test_xlsx_con_marca_estilos_y_tipos(client_usuario: AsyncClient) -> None:
    m = marca()
    hoy = date.today()
    for i in range(3):
        await crear_convocatoria_en_bd(
            f"Ayudas {m} n{i}", organo_convocante="Ministerio de Pruebas", nivel_administracion="estado",
            fecha_inicio_solicitud=hoy,
            fecha_fin_solicitud=hoy + timedelta(days=10 + i), presupuesto_total=1234567.5,
            url_portal_oficial="https://www.ejemplo.es/convocatoria/1",
        )  # fmt: skip
    libro, _ = await _xlsx(client_usuario, q=m, nivel="estado")
    hoja = libro.active
    assert hoja is not None and hoja.title == "Convocatorias"

    assert hoja["A1"].value == "Subvfy — Convocatorias"
    assert hoja["A1"].font.bold and hoja["A1"].font.color.rgb.endswith("42429A")
    assert "Exportado el" in hoja["A2"].value and "Fuente: BDNS" in hoja["A2"].value
    assert m in hoja["A3"].value and "Nivel: estado" in hoja["A3"].value

    cabecera = [c.value for c in hoja[5]]
    assert cabecera[:7] == [
        "Código BDNS", "Título", "Organismo", "Estado", "Inicio de solicitud", "Fin de solicitud", "Presupuesto",
    ]  # fmt: skip
    for celda in hoja[5]:
        assert celda.fill.start_color.rgb.endswith("42429A")
        assert celda.font.bold and celda.font.color.rgb.endswith("FFFFFF")
    assert hoja.freeze_panes == "A6"
    assert hoja.auto_filter.ref and hoja.auto_filter.ref.startswith("A5:")
    assert hoja.column_dimensions["B"].width >= 50

    # Real types: dates and money are cells with number formats, not text.
    fila = hoja[6]
    assert fila[0].value.startswith("TST")
    assert isinstance(fila[5].value, datetime) and fila[5].number_format == "dd/mm/yyyy"
    assert fila[6].value == 1234567.5 and "€" in fila[6].number_format
    assert fila[1].alignment.wrap_text and fila[2].value == "Ministerio de Pruebas"
    # Alternating tint: the second data row is tinted, the first and third are not.
    assert hoja[7][0].fill.fill_type == "solid"
    assert hoja[6][0].fill.fill_type is None and hoja[8][0].fill.fill_type is None
    # Portal URL is a real hyperlink.
    assert fila[14].hyperlink is not None and fila[14].hyperlink.target == "https://www.ejemplo.es/convocatoria/1"

    propiedades = libro.properties
    assert propiedades.creator == "Subvfy" and propiedades.title == "Subvfy — Convocatorias"
    assert m in (propiedades.description or "")


async def test_xlsx_no_ejecuta_formulas_ni_enlaces_peligrosos(client_usuario: AsyncClient) -> None:
    m = marca()
    await crear_convocatoria_en_bd(
        f"=HYPERLINK(1) {m}", organo_convocante="+cmd", url_portal_oficial='=HYPERLINK("http://x")'
    )
    await crear_convocatoria_en_bd(f"Otra {m}", url_portal_oficial="javascript:alert(1)")
    await crear_convocatoria_en_bd(f"Tercera {m}", url_portal_oficial="@SUM(1)")
    libro, _ = await _xlsx(client_usuario, q=m)
    hoja = libro.active
    assert hoja is not None
    for fila in hoja.iter_rows(min_row=6):
        for celda in fila:
            assert celda.data_type != "f", celda.coordinate
            if isinstance(celda.value, str):
                assert not celda.value.startswith(("=", "+", "@"))
        assert fila[14].hyperlink is None  # none of these is a plain http(s) URL
    titulos = {f[1].value for f in hoja.iter_rows(min_row=6)}
    assert f"'=HYPERLINK(1) {m}" in titulos


async def test_la_linea_de_filtros_neutraliza_la_busqueda(client_usuario: AsyncClient) -> None:
    libro, _ = await _xlsx(client_usuario, q="=1+1")
    hoja = libro.active
    assert hoja is not None and hoja["A3"].data_type != "f"


# --- Abuse limits -----------------------------------------------------------------------------


async def test_cabeceras_de_truncado(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    m = marca()
    await sembrar(m, 5)
    monkeypatch.setattr(exportacion, "MAX_FILAS", 3)
    r = await client_usuario.get("/convocatorias/export", params={"formato": "csv", "q": m})
    assert r.headers["x-filas-exportadas"] == "3"
    assert r.headers["x-filas-total"] == "5"
    assert r.headers["x-export-truncado"] == "true"
    monkeypatch.setattr(exportacion, "MAX_FILAS", 50)
    r = await client_usuario.get("/convocatorias/export", params={"formato": "csv", "q": m})
    assert r.headers["x-export-truncado"] == "false"


def test_los_limites_por_defecto() -> None:
    assert (settings.export_max_filas, settings.export_max_diarias_usuario) == (5000, 20)
    assert settings.listado_max_por_minuto == 120


async def test_limite_diario_de_exportaciones_y_registro(
    client_usuario: AsyncClient, client_gestor: AsyncClient, usuario_raso: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "export_max_diarias_usuario", 2)
    m = marca()
    await sembrar(m, 2)
    assert (await client_usuario.get("/convocatorias/export", params={"formato": "csv", "q": m})).status_code == 200
    assert (await client_usuario.get("/convocatorias/export", params={"formato": "xlsx", "q": m})).status_code == 200
    r = await client_usuario.get("/convocatorias/export", params={"formato": "csv", "q": m})
    assert r.status_code == 429
    assert "2 exportaciones al día" in r.json()["detail"]
    # The limit is per user: a colleague still can.
    assert (await client_gestor.get("/convocatorias/export", params={"formato": "csv", "q": m})).status_code == 200

    async with AsyncSessionLocal() as db:
        consulta = select(ExportacionLog).where(ExportacionLog.usuario_id == usuario_raso.id)
        consulta = consulta.order_by(ExportacionLog.id)
        registros = (await db.scalars(consulta)).all()
    assert [(r.formato, r.filas, r.empresa_id) for r in registros] == [
        ("csv", 2, usuario_raso.empresa_id),
        ("xlsx", 2, usuario_raso.empresa_id),
    ]
    assert registros[0].filtros["q"] == m


async def test_los_exports_de_ayer_no_cuentan(
    client_usuario: AsyncClient, usuario_raso: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "export_max_diarias_usuario", 1)
    async with AsyncSessionLocal() as db:
        db.add(
            ExportacionLog(
                usuario_id=usuario_raso.id, empresa_id=usuario_raso.empresa_id, formato="csv", filtros={}, filas=1,
                created_at=func.now() - timedelta(days=2),
            )  # fmt: skip
        )
        await db.commit()
    assert (await client_usuario.get("/convocatorias/export", params={"formato": "csv"})).status_code == 200
    assert (await client_usuario.get("/convocatorias/export", params={"formato": "csv"})).status_code == 429


async def test_el_listado_tiene_limite_por_usuario_y_minuto(
    client_usuario: AsyncClient, client_gestor: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.routes import convocatorias as ruta

    monkeypatch.setattr(ruta, "limite_listado", LimiteIntentos(maximo=3, ventana=60))
    for _ in range(3):
        assert (await client_usuario.get("/convocatorias", params={"size": 1})).status_code == 200
    r = await client_usuario.get("/convocatorias", params={"size": 1})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert (await client_gestor.get("/convocatorias", params={"size": 1})).status_code == 200  # other user
