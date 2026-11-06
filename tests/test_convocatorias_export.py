"""Authenticated export of the filtered result set (PR9, tasks 9.1)."""

import csv
import io
import random
import string
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook

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
    assert hoja.max_row == 13


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
