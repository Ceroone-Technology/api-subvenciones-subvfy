"""Un error no controlado devuelve JSON 500 con cabeceras CORS (si no, el navegador ve un fallo de red)."""

import logging

import httpx
import pytest
from fastapi import APIRouter

from app.config import settings
from app.main import app

ruta = APIRouter()


@ruta.get("/__test_explota__")
async def explota() -> None:
    raise RuntimeError("secreto-" + "personal@example.com")  # concatenated: the traceback prints source lines


@pytest.fixture
async def cliente_sin_reraise():
    app.include_router(ruta)
    transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transporte, base_url="http://test") as c:
            yield c
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__test_explota__"]


async def test_500_con_json_y_cors(cliente_sin_reraise, caplog):
    origen = settings.cors_origins_list[0]
    with caplog.at_level(logging.ERROR, logger="app.errores"):
        r = await cliente_sin_reraise.get("/__test_explota__", headers={"Origin": origen})
    assert r.status_code == 500
    assert r.headers["access-control-allow-origin"] == origen
    assert r.headers["access-control-allow-credentials"] == "true"
    assert r.json() == {"detail": "Ha ocurrido un error inesperado. Inténtalo de nuevo más tarde."}
    assert "secreto-personal" not in r.text
    assert "RuntimeError" in caplog.text
    assert "secreto-personal" not in caplog.text


async def test_500_no_da_cors_a_un_origen_no_permitido(cliente_sin_reraise):
    r = await cliente_sin_reraise.get("/__test_explota__", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers
    assert r.json()["detail"].startswith("Ha ocurrido")
