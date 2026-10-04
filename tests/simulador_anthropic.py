"""Simulador de Anthropic para los tests del Análisis con IA (Hito 5, H5.2).

`AsyncAnthropic` real sobre un `httpx.MockTransport`: se prueba el SDK de
verdad y solo se simula el servicio. Contesta según la herramienta que pide
cada llamada, así que las llamadas en paralelo reciben cada una lo suyo.
Lo usan `test_analisis_ia.py` y `test_cli_probar_analisis.py`.
"""

import json
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from app.services.ia_cliente import ClienteIA

CLAVE_FALSA = "sk-ant-clave-de-prueba-no-real"

RESPUESTAS = {
    "registrar_resumen": {
        "resumen": "Convenio del Ayuntamiento de Castellón para la feria TROVAM.",
        "finalidad": "Cultura.",
        "a_quien_va": "Una asociación concreta.",
        "puntos_clave": ["25.000 €", "Concesión directa"],
    },
    "registrar_requisitos_clave": {
        "beneficiarios": ["Personas jurídicas sin actividad económica."],
        "plazos": ["Del 2025-02-13 al 2025-12-31."],
        "informacion_insuficiente": False,
        # La IA no puede poner el aviso: no está en su formato y se ignora.
        "aviso_bases": "La IA intenta cambiar el aviso.",
    },
    "registrar_idoneidad": {
        "encaje": "alto",
        "motivos_a_favor": ["Sector cultural."],
        "explicacion": "Encaja con su actividad.",
    },
}


def _respuesta(herramienta: str, datos: dict[str, Any] | None = None) -> httpx.Response:
    entrada = datos or RESPUESTAS[herramienta]
    return httpx.Response(
        200,
        json={
            "id": "msg_prueba",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-20260101",
            "content": [
                {"type": "tool_use", "id": "toolu_prueba", "name": herramienta, "input": entrada}
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        },
    )


class SimuladorAnthropic:
    """Contesta según la herramienta pedida y guarda los cuerpos recibidos."""

    def __init__(self, *, datos: dict[str, dict[str, Any]] | None = None, fallos: dict[str, int] | None = None):
        self.datos = datos or {}
        self.fallos = fallos or {}
        self.cuerpos: dict[str, dict[str, Any]] = {}

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(peticion.content)
        herramienta = cuerpo["tool_choice"]["name"]
        self.cuerpos[herramienta] = cuerpo
        if herramienta in self.fallos:
            return httpx.Response(self.fallos[herramienta], json={"type": "error", "error": {}})
        return _respuesta(herramienta, self.datos.get(herramienta))

    def cliente(self) -> ClienteIA:
        return ClienteIA(
            AsyncAnthropic(
                api_key=CLAVE_FALSA,
                base_url="https://anthropic.example",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(self)),
            )
        )
