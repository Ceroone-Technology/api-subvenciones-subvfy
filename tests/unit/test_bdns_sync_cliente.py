"""Sync client of the BDNS (PR2 of `bdns-local-sync`, tasks 2.1-2.3).

The BDNS is never called: an `httpx.MockTransport` serves recorded fixtures
(`tests/fixtures/bdns/`) and the clock/sleep/jitter are injected, so throttle,
backoff and breaker are tested without waiting.
"""

from datetime import date

import httpx
import pytest

from app.services.bdns_cliente import BdnsNoDisponible
from app.services.bdns_sync_cliente import CircuitoAbierto, ClienteBdnsSync, inferir_direccion, orden_de_fechas
from tests.fixtures_bdns import cargar_fixture


class RelojFalso:
    """Monotonic clock whose `dormir` advances time instead of waiting."""

    def __init__(self) -> None:
        self.ahora = 1000.0
        self.esperas: list[float] = []

    def __call__(self) -> float:
        return self.ahora

    async def dormir(self, segundos: float) -> None:
        self.esperas.append(segundos)
        self.ahora += segundos


class Servidor:
    """MockTransport handler that records requests (and when they arrived)."""

    def __init__(self, reloj: RelojFalso, respuestas: list[httpx.Response | Exception]) -> None:
        self.reloj = reloj
        self.respuestas = list(respuestas)
        self.peticiones: list[httpx.Request] = []
        self.instantes: list[float] = []

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion)
        self.instantes.append(self.reloj.ahora)
        siguiente = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente


def crear(
    respuestas: list[httpx.Response | Exception], **opciones: object
) -> tuple[ClienteBdnsSync, Servidor, RelojFalso]:
    reloj = RelojFalso()
    servidor = Servidor(reloj, respuestas)
    cliente = ClienteBdnsSync(
        httpx.AsyncClient(transport=httpx.MockTransport(servidor), base_url="https://bdns.example/api"),
        reloj=reloj,
        dormir=reloj.dormir,
        aleatorio=lambda: 0.0,
        **opciones,  # type: ignore[arg-type]
    )
    return cliente, servidor, reloj


def ok(cuerpo: object = None) -> httpx.Response:
    return httpx.Response(200, json={"content": []} if cuerpo is None else cuerpo)


HTML_WAF = httpx.Response(
    200, text="<html><body>ticket de soporte</body></html>", headers={"content-type": "text/html"}
)


def llamar_listado(cliente: ClienteBdnsSync):  # noqa: ANN201
    return cliente.listar_pagina(date(2026, 8, 1), date(2026, 8, 7), pagina=0)


# --- 2.1 The list call walks ascending ------------------------------------


async def test_el_listado_pide_direccion_asc_y_se_parsea_la_fixture_grabada() -> None:
    cliente, servidor, _ = crear([ok(cargar_fixture("busqueda_real.json"))])

    pagina = await cliente.listar_pagina(date(2026, 8, 1), date(2026, 8, 7), pagina=2)

    parametros = servidor.peticiones[0].url.params
    assert servidor.peticiones[0].url.path == "/api/convocatorias/busqueda"
    assert parametros["direccion"] == "asc"
    assert parametros["order"] == "fechaRecepcion"
    assert parametros["pageSize"] == "200"
    assert parametros["page"] == "2"
    assert parametros["fechaDesde"] == "01/08/2026"
    assert parametros["fechaHasta"] == "07/08/2026"
    assert parametros["vpd"] == "GE"
    assert [c.codigo_bdns for c in pagina.convocatorias] == ["924160", "924159", "924158"]
    assert pagina.convocatorias[0].nivel2 == "PRINCIPADO DE ASTURIAS"
    assert pagina.ultima is False


async def test_la_direccion_se_puede_cambiar_para_el_plan_b_desc() -> None:
    cliente, servidor, _ = crear([ok()])

    await cliente.listar_pagina(date(2026, 8, 1), date(2026, 8, 7), pagina=0, direccion="desc", tamano=50)

    assert servidor.peticiones[0].url.params["direccion"] == "desc"
    assert servidor.peticiones[0].url.params["pageSize"] == "50"


@pytest.mark.parametrize(
    ("fechas", "esperado"),
    [
        ([date(2026, 8, 1), date(2026, 8, 1), date(2026, 8, 3)], "asc"),
        ([date(2026, 8, 5), date(2026, 8, 3), date(2026, 8, 3)], "desc"),
        ([date(2026, 8, 3), date(2026, 8, 1), date(2026, 8, 2)], "mixto"),
        ([date(2026, 8, 7), date(2026, 8, 7)], "constante"),
    ],
)
def test_orden_de_fechas_distingue_asc_desc_mixto_y_constante(fechas: list[date], esperado: str) -> None:
    assert orden_de_fechas(fechas) == esperado


def test_la_pagina_grabada_tiene_todas_las_fechas_iguales() -> None:
    """The recorded page cannot prove the order, which is why the guarded live
    check (`scripts/check_bdns_asc.py`) exists."""
    fechas = [date.fromisoformat(f["fechaRecepcion"]) for f in cargar_fixture("busqueda_real.json")["content"]]
    assert orden_de_fechas(fechas) == "constante"


# --- Defaults of the real client ------------------------------------------


def test_el_cliente_por_defecto_lleva_timeouts_y_user_agent_de_la_spec() -> None:
    cliente = ClienteBdnsSync()
    interno = cliente._cliente
    assert interno.timeout.connect == 5.0
    assert interno.timeout.read == 30.0
    assert interno.headers["User-Agent"] == "Subvfy-sync/1.0"
    assert interno.headers["Accept"] == "application/json"


# --- 2.2 Token bucket -----------------------------------------------------


async def test_el_throttle_separa_las_peticiones_al_menos_1_sobre_rps() -> None:
    cliente, servidor, _ = crear([ok()], rps=3.0)

    for _ in range(6):
        await llamar_listado(cliente)

    huecos = [b - a for a, b in zip(servidor.instantes, servidor.instantes[1:], strict=False)]
    assert len(huecos) == 5
    assert all(hueco >= 1 / 3 - 1e-6 for hueco in huecos)
    assert servidor.instantes[1] - servidor.instantes[0] < 0.5  # no more than needed


@pytest.mark.parametrize(("pedido", "efectivo"), [(10.0, 4.0), (0.5, 2.0), (3.0, 3.0)])
async def test_el_rps_se_acota_entre_2_y_4(pedido: float, efectivo: float) -> None:
    cliente, servidor, _ = crear([ok()], rps=pedido)

    for _ in range(3):
        await llamar_listado(cliente)

    hueco = servidor.instantes[2] - servidor.instantes[1]
    assert hueco == pytest.approx(1 / efectivo, abs=1e-6)


async def test_el_primer_request_no_espera() -> None:
    cliente, _, reloj = crear([ok()])

    await llamar_listado(cliente)

    assert reloj.esperas == []


# --- 2.2 Retry and backoff ------------------------------------------------


async def test_429_se_reintenta_con_backoff_y_acaba_bien() -> None:
    cliente, servidor, reloj = crear(
        [httpx.Response(429), httpx.Response(429), ok(cargar_fixture("busqueda_real.json"))]
    )

    pagina = await llamar_listado(cliente)

    assert len(pagina.convocatorias) == 3
    assert len(servidor.peticiones) == 3
    assert cliente.peticiones == 3
    # 2 s, then 4 s (the bucket refill is shorter than the backoff, so it adds nothing)
    assert reloj.esperas == [2.0, 4.0]


async def test_retry_after_manda_sobre_el_backoff_si_es_mayor() -> None:
    cliente, _, reloj = crear([httpx.Response(429, headers={"Retry-After": "7"}), ok()])

    await llamar_listado(cliente)

    assert reloj.esperas == [7.0]


async def test_retry_after_pequeno_no_baja_del_backoff() -> None:
    cliente, _, reloj = crear([httpx.Response(429, headers={"Retry-After": "0"}), ok()])

    await llamar_listado(cliente)

    assert reloj.esperas == [2.0]


async def test_un_retry_after_ilegible_se_ignora() -> None:
    cliente, _, reloj = crear([httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), ok()])

    await llamar_listado(cliente)

    assert reloj.esperas == [2.0]


async def test_el_jitter_se_suma_al_backoff() -> None:
    reloj = RelojFalso()
    servidor = Servidor(reloj, [httpx.Response(500), ok()])
    cliente = ClienteBdnsSync(
        httpx.AsyncClient(transport=httpx.MockTransport(servidor), base_url="https://bdns.example/api"),
        reloj=reloj,
        dormir=reloj.dormir,
        aleatorio=lambda: 1.0,
    )

    await llamar_listado(cliente)

    assert reloj.esperas == [2.5]  # 2 s + 25 % of jitter


async def test_el_backoff_dobla_y_se_acota_a_60_segundos() -> None:
    cliente, _, reloj = crear([httpx.Response(500)], intentos=8, umbral_breaker=99)

    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)

    assert reloj.esperas == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


@pytest.mark.parametrize("estado", [500, 502, 503, 504, 429])
async def test_los_estados_transitorios_se_reintentan(estado: int) -> None:
    cliente, servidor, _ = crear([httpx.Response(estado), ok()])

    await llamar_listado(cliente)

    assert len(servidor.peticiones) == 2


async def test_un_timeout_se_reintenta() -> None:
    cliente, servidor, reloj = crear([httpx.ReadTimeout("lento"), ok()])

    await llamar_listado(cliente)

    assert len(servidor.peticiones) == 2
    assert reloj.esperas == [2.0]


async def test_un_error_de_transporte_se_reintenta() -> None:
    cliente, servidor, _ = crear([httpx.ConnectError("sin ruta"), ok()])

    await llamar_listado(cliente)

    assert len(servidor.peticiones) == 2


async def test_un_200_html_del_waf_se_reintenta_como_fallo() -> None:
    cliente, servidor, reloj = crear([HTML_WAF, HTML_WAF, ok(cargar_fixture("busqueda_real.json"))])

    pagina = await llamar_listado(cliente)

    assert len(pagina.convocatorias) == 3
    assert len(servidor.peticiones) == 3
    assert reloj.esperas == [2.0, 4.0]


async def test_un_200_con_cuerpo_vacio_no_es_json_y_falla() -> None:
    cliente, servidor, _ = crear([httpx.Response(200, content=b"")], intentos=2)

    with pytest.raises(BdnsNoDisponible, match="JSON"):
        await llamar_listado(cliente)

    assert len(servidor.peticiones) == 2


async def test_agotados_los_intentos_se_lanza_bdns_no_disponible_con_el_estado() -> None:
    cliente, servidor, _ = crear([httpx.Response(429)], intentos=4)

    with pytest.raises(BdnsNoDisponible) as error:
        await llamar_listado(cliente)

    assert error.value.status == 429
    assert len(servidor.peticiones) == 4
    assert cliente.peticiones == 4


async def test_todos_los_intentos_incluidos_los_reintentos_respetan_el_throttle() -> None:
    cliente, servidor, _ = crear(
        [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(429), ok()], rps=2.0
    )

    await llamar_listado(cliente)

    huecos = [b - a for a, b in zip(servidor.instantes, servidor.instantes[1:], strict=False)]
    assert len(servidor.instantes) == 3
    assert all(hueco >= 0.5 - 1e-6 for hueco in huecos)


async def test_un_4xx_no_transitorio_no_se_reintenta() -> None:
    cliente, servidor, reloj = crear([httpx.Response(400)])

    with pytest.raises(BdnsNoDisponible) as error:
        await llamar_listado(cliente)

    assert error.value.status == 400
    assert len(servidor.peticiones) == 1
    assert reloj.esperas == []


async def test_una_pagina_con_forma_inesperada_falla_sin_reintentar() -> None:
    cliente, servidor, _ = crear([ok(["no", "es", "una", "pagina"])])

    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)

    assert len(servidor.peticiones) == 1


# --- 2.2 Circuit breaker --------------------------------------------------


async def test_el_breaker_salta_tras_k_fallos_consecutivos_y_ya_no_pide_nada() -> None:
    cliente, servidor, _ = crear([httpx.Response(503)], intentos=1, umbral_breaker=3)

    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)
    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)
    with pytest.raises(CircuitoAbierto):
        await llamar_listado(cliente)
    pedidas = len(servidor.peticiones)
    with pytest.raises(CircuitoAbierto):
        await llamar_listado(cliente)

    assert pedidas == 3
    assert len(servidor.peticiones) == 3
    assert cliente.circuito_abierto is True


async def test_circuito_abierto_no_es_un_bdns_no_disponible() -> None:
    """The engine catches `BdnsNoDisponible` per item and keeps going; the
    breaker must escape that handler to end the run."""
    assert not issubclass(CircuitoAbierto, BdnsNoDisponible)


async def test_un_exito_reinicia_la_cuenta_de_fallos() -> None:
    cliente, _, _ = crear(
        [httpx.Response(503), httpx.Response(503), ok(), httpx.Response(503), httpx.Response(503)],
        intentos=1,
        umbral_breaker=3,
    )

    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)
    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)
    await llamar_listado(cliente)
    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)
    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)

    assert cliente.circuito_abierto is False


async def test_los_reintentos_de_una_misma_llamada_cuentan_como_un_solo_fallo() -> None:
    """Timeout spec: an item whose retries are exhausted is one failure; the run
    continues until K such failures."""
    cliente, servidor, _ = crear([httpx.ReadTimeout("lento")], intentos=4, umbral_breaker=2)

    with pytest.raises(BdnsNoDisponible):
        await llamar_listado(cliente)

    assert len(servidor.peticiones) == 4
    assert cliente.circuito_abierto is False
    with pytest.raises(CircuitoAbierto):
        await llamar_listado(cliente)


# --- 2.2 Detail, catalogs --------------------------------------------------


async def test_detalle_pide_num_conv_y_vpd_y_devuelve_el_json() -> None:
    cliente, servidor, _ = crear([ok(cargar_fixture("detalle_con_documentos_real.json"))])

    detalle = await cliente.detalle("924183")

    assert servidor.peticiones[0].url.path == "/api/convocatorias"
    assert servidor.peticiones[0].url.params["numConv"] == "924183"
    assert servidor.peticiones[0].url.params["vpd"] == "GE"
    assert detalle is not None
    assert detalle["documentos"][0]["id"] == 1507071


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(404),
        httpx.Response(204),
        httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
    ],
)
async def test_detalle_inexistente_es_none(respuesta: httpx.Response) -> None:
    cliente, _, _ = crear([respuesta])

    assert await cliente.detalle("000000") is None


async def test_detalle_vacio_200_con_objeto_vacio_es_none() -> None:
    cliente, _, _ = crear([httpx.Response(200, json={})])

    assert await cliente.detalle("000000") is None


async def test_un_404_no_cuenta_para_el_breaker_y_no_se_reintenta() -> None:
    cliente, servidor, _ = crear([httpx.Response(404)], umbral_breaker=2)

    for _ in range(4):
        assert await cliente.detalle("000000") is None

    assert len(servidor.peticiones) == 4
    assert cliente.circuito_abierto is False


async def test_el_404_del_listado_si_es_un_fallo() -> None:
    cliente, _, _ = crear([httpx.Response(404)])

    with pytest.raises(BdnsNoDisponible) as error:
        await llamar_listado(cliente)

    assert error.value.status == 404


async def test_organos_pide_id_admon_y_devuelve_el_arbol() -> None:
    cliente, servidor, _ = crear([ok(cargar_fixture("organos_estado_real.json"))])

    arbol = await cliente.organos("C")

    assert servidor.peticiones[0].url.path == "/api/organos"
    assert servidor.peticiones[0].url.params["idAdmon"] == "C"
    assert servidor.peticiones[0].url.params["vpd"] == "GE"
    assert arbol[0]["id"] == 23
    assert len(arbol[0]["children"]) == 10


async def test_regiones_devuelve_el_arbol_nuts() -> None:
    cliente, servidor, _ = crear([ok(cargar_fixture("regiones_real_recortado.json"))])

    arbol = await cliente.regiones()

    assert servidor.peticiones[0].url.path == "/api/regiones"
    assert servidor.peticiones[0].url.params["vpd"] == "GE"
    assert arbol[0]["id"] == 1


async def test_un_catalogo_que_no_es_una_lista_falla() -> None:
    cliente, _, _ = crear([ok({"content": []})])

    with pytest.raises(BdnsNoDisponible):
        await cliente.regiones()


# --- 2.1 Verdict of the guarded live check --------------------------------

DESDE = date(2026, 10, 13)
HASTA = date(2026, 10, 23)


@pytest.mark.parametrize(
    ("fechas", "hay_mas_paginas", "esperado"),
    [
        # Strict order inside the page.
        ([date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 14)], False, "asc"),
        ([date(2026, 10, 22), date(2026, 10, 20)], True, "desc"),
        # Observed live on 22/10/2026: 20 rows, all on the window start, 126 pages.
        ([DESDE] * 20, True, "asc"),
        ([HASTA] * 20, True, "desc"),
        # A constant page in the middle of the window proves nothing.
        ([date(2026, 10, 18)] * 20, True, "indeterminado"),
        # A single page on the window edge could be the whole window.
        ([DESDE] * 5, False, "indeterminado"),
        ([date(2026, 10, 14), date(2026, 10, 13), date(2026, 10, 15)], True, "indeterminado"),
        ([], False, "indeterminado"),
    ],
)
def test_inferir_direccion_con_una_sola_pagina(fechas: list[date], hay_mas_paginas: bool, esperado: str) -> None:
    assert inferir_direccion(fechas, DESDE, HASTA, hay_mas_paginas=hay_mas_paginas) == esperado
