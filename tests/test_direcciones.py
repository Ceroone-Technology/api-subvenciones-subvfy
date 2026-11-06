"""Address search (CartoCiudad proxy). The upstream is always mocked: no test touches the network."""

import logging

import httpx
import pytest
from httpx import AsyncClient

from app.api.routes.direcciones import limite_busquedas
from app.services import direcciones as servicio

FILA = {
    "address": "Calle Mayor, Madrid",
    "muni": "Madrid",
    "province": "Madrid",
    "postalCode": "28013",
    "comunidadAutonoma": "Comunidad de Madrid",
    "comunidadAutonomaCode": "13",
    "type": "portal",
    "lat": 40.4,
    "lng": -3.7,
}


@pytest.fixture(autouse=True)
def _limpio() -> None:
    servicio.cache.vaciar()
    limite_busquedas._fallos.clear()


def _upstream(monkeypatch: pytest.MonkeyPatch, manejador) -> list[httpx.Request]:
    vistas: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        return manejador(request)

    monkeypatch.setattr(servicio, "crear_cliente", lambda: httpx.AsyncClient(transport=httpx.MockTransport(responder)))
    return vistas


async def test_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/direcciones/buscar", params={"q": "calle mayor"})).status_code == 401


@pytest.mark.parametrize("q", ["ab", "  a ", "", "x" * 151])
async def test_query_length_is_validated(client_usuario: AsyncClient, q: str) -> None:
    assert (await client_usuario.get("/direcciones/buscar", params={"q": q})).status_code == 422


async def test_limit_is_validated(client_usuario: AsyncClient) -> None:
    for limit in (0, 11):
        r = await client_usuario.get("/direcciones/buscar", params={"q": "mayor", "limit": limit})
        assert r.status_code == 422


async def test_maps_the_fields_for_any_role(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    vistas = _upstream(monkeypatch, lambda r: httpx.Response(200, json=[FILA, {"address": "Otra", "muni": None}]))
    r = await client_usuario.get("/direcciones/buscar", params={"q": "  calle   mayor 1 ", "limit": 3})
    assert r.status_code == 200
    assert r.json() == [
        {
            "etiqueta": "Calle Mayor, Madrid", "municipio": "Madrid", "provincia": "Madrid",
            "codigo_postal": "28013", "comunidad": "Comunidad de Madrid", "tipo": "portal",
        },
        dict.fromkeys(("municipio", "provincia", "codigo_postal", "comunidad", "tipo")) | {"etiqueta": "Otra"},
    ]  # fmt: skip
    assert vistas[0].url.path.endswith("/candidates")
    assert vistas[0].url.params["q"] == "calle mayor 1" and vistas[0].url.params["limit"] == "3"


async def test_default_limit_is_five_and_extra_rows_are_cut(
    client_gestor: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    vistas = _upstream(monkeypatch, lambda r: httpx.Response(200, json=[FILA] * 9))
    r = await client_gestor.get("/direcciones/buscar", params={"q": "mayor"})
    assert vistas[0].url.params["limit"] == "5" and len(r.json()) == 5


async def test_empty_result_is_an_empty_list(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _upstream(monkeypatch, lambda r: httpx.Response(200, json=[]))
    r = await client_usuario.get("/direcciones/buscar", params={"q": "zzzzzz"})
    assert r.status_code == 200 and r.json() == []


async def test_rows_without_address_are_skipped(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _upstream(monkeypatch, lambda r: httpx.Response(200, json=[{"muni": "X"}, {"address": "  "}, FILA]))
    assert len((await client_usuario.get("/direcciones/buscar", params={"q": "mayor"})).json()) == 1


async def test_timeout_is_503(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def tarda(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    _upstream(monkeypatch, tarda)
    r = await client_usuario.get("/direcciones/buscar", params={"q": "mayor"})
    assert r.status_code == 503 and "a mano" in r.json()["detail"]


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, text="<html>no es json</html>"),
        httpx.Response(200, json={"error": "formato nuevo"}),
        httpx.Response(200, json=["texto", 3]),
        httpx.Response(500, text="boom"),
        httpx.Response(404, json=[]),
    ],
)
async def test_malformed_or_failing_upstream_is_503(
    client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch, respuesta: httpx.Response
) -> None:
    _upstream(monkeypatch, lambda r: respuesta)
    assert (await client_usuario.get("/direcciones/buscar", params={"q": "mayor"})).status_code == 503


async def test_network_error_is_503(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def cae(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    _upstream(monkeypatch, cae)
    assert (await client_usuario.get("/direcciones/buscar", params={"q": "mayor"})).status_code == 503


async def test_failures_are_not_cached_but_successes_are(
    client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    estado = {"ok": False}
    vistas = _upstream(monkeypatch, lambda r: httpx.Response(200, json=[FILA]) if estado["ok"] else httpx.Response(500))
    assert (await client_usuario.get("/direcciones/buscar", params={"q": "Mayor"})).status_code == 503
    estado["ok"] = True
    assert (await client_usuario.get("/direcciones/buscar", params={"q": "Mayor"})).status_code == 200
    assert (await client_usuario.get("/direcciones/buscar", params={"q": "MAYOR"})).status_code == 200  # cached
    assert len(vistas) == 2


async def test_cache_is_bounded(client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(servicio, "cache", servicio._Cache(2, 3600))
    _upstream(monkeypatch, lambda r: httpx.Response(200, json=[FILA]))
    for texto in ("uno uno", "dos dos", "tres tres"):
        await client_usuario.get("/direcciones/buscar", params={"q": texto})
    assert len(servicio.cache) == 2


async def test_cache_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = servicio._Cache(4, ttl=-1)
    cache.guardar(("a", 5), [])
    assert cache.obtener(("a", 5)) is None


async def test_per_user_rate_limit(
    client_usuario: AsyncClient, client_gestor: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upstream(monkeypatch, lambda r: httpx.Response(200, json=[FILA]))
    for _ in range(30):
        assert (await client_usuario.get("/direcciones/buscar", params={"q": "mayor"})).status_code == 200
    assert (await client_usuario.get("/direcciones/buscar", params={"q": "mayor"})).status_code == 429
    assert (await client_gestor.get("/direcciones/buscar", params={"q": "mayor"})).status_code == 200  # other user


async def test_the_query_is_not_logged_at_info(
    client_usuario: AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _upstream(monkeypatch, lambda r: httpx.Response(500))
    with caplog.at_level(logging.INFO):
        await client_usuario.get("/direcciones/buscar", params={"q": "calle-secreta-77"})
    # The test client logs its own request line (http://test/...); what must not appear is the query in the app or
    # in the upstream call.
    mensajes = [r.getMessage() for r in caplog.records]
    filtradas = [m for m in mensajes if "calle-secreta-77" in m and "http://test" not in m]
    assert filtradas == []
