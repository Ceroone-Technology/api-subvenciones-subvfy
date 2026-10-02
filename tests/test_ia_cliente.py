"""Cliente de Anthropic (Hito 5, tarea H5.1).

**No se llama a Anthropic real**: se inyecta un `AsyncAnthropic` montado sobre
un `httpx.MockTransport`, igual que en los tests del cliente de la BDNS. Así
se prueba el SDK de verdad (serialización, códigos de error, reintentos) y
solo se simula el servicio externo. No hace falta base de datos.
"""

import json
import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic

from app.config import settings
from app.services.ia_cliente import (
    ClienteIA,
    IANoConfigurada,
    IANoDisponible,
    IARespuestaInvalida,
    calcular_coste,
)

CLAVE_FALSA = "sk-ant-clave-de-prueba-no-real"
SISTEMA = "Eres un analista de subvenciones."
# Algo reconocible para comprobar que el prompt no se filtra a errores ni logs.
MENSAJE = "Perfil de la empresa TEST-CONFIDENCIAL-1234 y ficha de la convocatoria."


def _mensaje(
    texto: str = '{"resumen": "ok"}', *, stop_reason: str = "end_turn", entrada: int = 120, salida: int = 45
) -> dict[str, Any]:
    return {
        "id": "msg_prueba",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-20260101",
        "content": [{"type": "text", "text": texto}] if texto else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": entrada, "output_tokens": salida},
    }


def _error(status: int, tipo: str = "api_error") -> httpx.Response:
    return httpx.Response(
        status,
        headers={"request-id": f"req_{status}"},
        json={"type": "error", "error": {"type": tipo, "message": f"eco del prompt: {MENSAJE}"}},
    )


def _cliente(manejador, *, max_reintentos: int = 0) -> ClienteIA:
    return ClienteIA(
        AsyncAnthropic(
            api_key=CLAVE_FALSA,
            base_url="https://anthropic.example",
            max_retries=max_reintentos,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(manejador)),
        )
    )


async def _generar(cliente: ClienteIA):
    async with cliente:
        return await cliente.generar(sistema=SISTEMA, mensaje=MENSAJE)


# --- Respuesta correcta ---------------------------------------------------


async def test_devuelve_texto_modelo_y_tokens() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_mensaje())

    respuesta = await _generar(_cliente(manejador))

    assert respuesta.texto == '{"resumen": "ok"}'
    # El modelo que contestó, que es el que se guarda en analisis_ia.
    assert respuesta.modelo == "claude-sonnet-5-20260101"
    assert (respuesta.tokens_entrada, respuesta.tokens_salida) == (120, 45)


async def test_la_peticion_lleva_modelo_sistema_mensaje_y_clave() -> None:
    peticiones: list[httpx.Request] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return httpx.Response(200, json=_mensaje())

    await _generar(_cliente(manejador))

    (peticion,) = peticiones
    assert peticion.url.path.endswith("/v1/messages")
    assert peticion.headers["x-api-key"] == CLAVE_FALSA
    cuerpo = json.loads(peticion.content)
    assert cuerpo["model"] == settings.anthropic_model
    assert cuerpo["max_tokens"] == settings.anthropic_max_tokens
    assert cuerpo["system"] == SISTEMA
    assert cuerpo["messages"] == [{"role": "user", "content": MENSAJE}]


async def test_max_tokens_explicito_gana_a_la_configuracion() -> None:
    cuerpos: list[dict[str, Any]] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        cuerpos.append(json.loads(peticion.content))
        return httpx.Response(200, json=_mensaje())

    async with _cliente(manejador) as cliente:
        await cliente.generar(sistema=SISTEMA, mensaje=MENSAJE, max_tokens=300)

    assert cuerpos[0]["max_tokens"] == 300


async def test_une_varios_bloques_de_texto() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        cuerpo = _mensaje()
        cuerpo["content"] = [{"type": "text", "text": '{"a": '}, {"type": "text", "text": "1}"}]
        return httpx.Response(200, json=cuerpo)

    respuesta = await _generar(_cliente(manejador))

    assert respuesta.texto == '{"a": 1}'


# --- Sin configurar ---------------------------------------------------------


async def test_sin_clave_falla_al_generar_sin_llamar_a_nadie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    # Construirlo no falla: un endpoint que solo lee análisis guardados
    # tiene que funcionar sin IA configurada.
    async with ClienteIA() as cliente:
        with pytest.raises(IANoConfigurada, match="ANTHROPIC_API_KEY"):
            await cliente.generar(sistema=SISTEMA, mensaje=MENSAJE)


async def test_con_clave_construye_el_sdk_con_timeout_y_reintentos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", CLAVE_FALSA)
    monkeypatch.setattr(settings, "anthropic_timeout_segundos", 12.5)
    monkeypatch.setattr(settings, "anthropic_max_reintentos", 3)

    async with ClienteIA() as cliente:
        sdk = cliente._cliente
        assert isinstance(sdk, AsyncAnthropic)
        assert sdk.timeout == 12.5
        assert sdk.max_retries == 3


@pytest.mark.parametrize(
    ("status", "tipo_error", "mensaje"),
    [
        (401, "authentication_error", "credenciales"),
        (403, "permission_error", "credenciales"),
        (404, "not_found_error", "ANTHROPIC_MODEL"),
    ],
)
async def test_credenciales_o_modelo_invalidos_son_falta_de_configuracion(
    status: int, tipo_error: str, mensaje: str
) -> None:
    with pytest.raises(IANoConfigurada, match=mensaje) as error:
        await _generar(_cliente(lambda peticion: _error(status, tipo_error)))

    assert error.value.status == status
    assert error.value.request_id == f"req_{status}"


# --- No disponible ------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503, 529])
async def test_errores_transitorios_son_no_disponible(status: int) -> None:
    with pytest.raises(IANoDisponible) as error:
        await _generar(_cliente(lambda peticion: _error(status)))

    assert error.value.status == status


async def test_timeout_es_no_disponible() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("tiempo agotado", request=peticion)

    with pytest.raises(IANoDisponible, match="a tiempo"):
        await _generar(_cliente(manejador))


async def test_error_de_red_es_no_disponible() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=peticion)

    with pytest.raises(IANoDisponible, match="conectar"):
        await _generar(_cliente(manejador))


async def test_reintenta_un_429_y_acaba_bien() -> None:
    llamadas = 0

    def manejador(peticion: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            # retry-after-ms corto para que el backoff del SDK no alargue el test.
            return httpx.Response(429, headers={"retry-after-ms": "10"}, json={"type": "error", "error": {}})
        return httpx.Response(200, json=_mensaje())

    respuesta = await _generar(_cliente(manejador, max_reintentos=1))

    assert llamadas == 2
    assert respuesta.tokens_entrada == 120


async def test_sin_reintentos_no_insiste() -> None:
    llamadas = 0

    def manejador(peticion: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        return _error(503)

    with pytest.raises(IANoDisponible):
        await _generar(_cliente(manejador, max_reintentos=0))

    assert llamadas == 1


# --- Respuesta inválida --------------------------------------------------


async def test_respuesta_cortada_por_max_tokens_es_invalida() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_mensaje('{"resumen": "a medi', stop_reason="max_tokens"))

    with pytest.raises(IARespuestaInvalida, match="límite de tokens"):
        await _generar(_cliente(manejador))


async def test_respuesta_vacia_es_invalida() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_mensaje(""))

    with pytest.raises(IARespuestaInvalida, match="vacía"):
        await _generar(_cliente(manejador))


@pytest.mark.parametrize("status", [400, 413, 422])
async def test_peticion_rechazada_es_invalida(status: int) -> None:
    with pytest.raises(IARespuestaInvalida) as error:
        await _generar(_cliente(lambda peticion: _error(status, "invalid_request_error")))

    assert error.value.status == status


# --- Nada sensible en errores ni logs ------------------------------------


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_ni_clave_ni_prompt_salen_en_el_error_ni_en_el_log(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.services.ia_cliente")

    with pytest.raises(Exception) as error:
        # El cuerpo de error simulado hace eco del prompt: el mensaje propio
        # no debe arrastrarlo.
        await _generar(_cliente(lambda peticion: _error(status)))

    textos = [str(error.value), *(registro.getMessage() for registro in caplog.records)]
    for texto in textos:
        assert CLAVE_FALSA not in texto
        assert "TEST-CONFIDENCIAL" not in texto
        assert SISTEMA not in texto


async def test_el_log_de_exito_no_lleva_el_prompt_ni_la_respuesta(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="app.services.ia_cliente")

    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_mensaje('{"resumen": "TEST-CONFIDENCIAL-respuesta"}'))

    await _generar(_cliente(manejador))

    mensajes = " ".join(registro.getMessage() for registro in caplog.records)
    assert "120 tokens de entrada" in mensajes
    assert "TEST-CONFIDENCIAL" not in mensajes
    assert CLAVE_FALSA not in mensajes


# --- Coste -----------------------------------------------------------------


def test_coste_con_los_dos_precios(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_precio_entrada_millon", Decimal("3"))
    monkeypatch.setattr(settings, "anthropic_precio_salida_millon", Decimal("15"))

    # 1.000.000 × 3 + 200.000 × 15 = 6.000.000 → 6 USD.
    assert calcular_coste(1_000_000, 200_000) == Decimal("6.0000")
    # Redondeo a 4 decimales, como Numeric(10, 4): 1234 × 3 + 567 × 15 = 12.207 → 0,012207.
    assert calcular_coste(1234, 567) == Decimal("0.0122")


@pytest.mark.parametrize(("entrada", "salida"), [(None, None), (Decimal("3"), None), (None, Decimal("15"))])
def test_sin_algun_precio_no_hay_coste(
    entrada: Decimal | None, salida: Decimal | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "anthropic_precio_entrada_millon", entrada)
    monkeypatch.setattr(settings, "anthropic_precio_salida_millon", salida)

    assert calcular_coste(1000, 1000) is None
