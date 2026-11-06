"""Address search for Mi empresa: a proxy over the IGN geocoder CartoCiudad (no key).

Through the backend so the visitor IP never reaches a third party and the provider can be replaced. The query is
personal data (an address): it is never logged, and the in-memory cache keeps it for an hour at most. Any upstream
problem (timeout, network, HTTP error, unexpected body) becomes `DireccionesNoDisponible`, which the router answers
with 503 so the frontend falls back to manual entry.
"""

import logging
import time
from collections import OrderedDict
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

CACHE_MAX_ENTRADAS = 256
CACHE_TTL_SEGUNDOS = 3600


class DireccionesNoDisponible(Exception):
    """CartoCiudad did not answer usefully."""


class Direccion(BaseModel):
    etiqueta: str
    municipio: str | None
    provincia: str | None
    codigo_postal: str | None
    comunidad: str | None
    tipo: str | None


def crear_cliente() -> httpx.AsyncClient:
    """Replaced in tests with a client over `httpx.MockTransport`: no test calls the real service."""
    return httpx.AsyncClient(timeout=settings.cartociudad_timeout_segundos, follow_redirects=False)


class _Cache:
    def __init__(self, maximo: int, ttl: float) -> None:
        self._maximo, self._ttl = maximo, ttl
        self._datos: OrderedDict[tuple[str, int], tuple[float, list[Direccion]]] = OrderedDict()

    def obtener(self, clave: tuple[str, int]) -> list[Direccion] | None:
        entrada = self._datos.get(clave)
        if entrada is None:
            return None
        if time.monotonic() - entrada[0] > self._ttl:
            del self._datos[clave]
            return None
        self._datos.move_to_end(clave)
        return entrada[1]

    def guardar(self, clave: tuple[str, int], valor: list[Direccion]) -> None:
        self._datos[clave] = (time.monotonic(), valor)
        self._datos.move_to_end(clave)
        while len(self._datos) > self._maximo:
            self._datos.popitem(last=False)

    def vaciar(self) -> None:
        self._datos.clear()

    def __len__(self) -> int:
        return len(self._datos)


cache = _Cache(CACHE_MAX_ENTRADAS, CACHE_TTL_SEGUNDOS)


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _mapear(crudo: object, limite: int) -> list[Direccion]:
    if not isinstance(crudo, list):
        raise DireccionesNoDisponible("respuesta inesperada")
    resultado: list[Direccion] = []
    for fila in crudo:
        if not isinstance(fila, dict):
            raise DireccionesNoDisponible("respuesta inesperada")
        etiqueta = _texto(fila.get("address"))
        if etiqueta is None:
            continue
        resultado.append(
            Direccion(
                etiqueta=etiqueta,
                municipio=_texto(fila.get("muni")),
                provincia=_texto(fila.get("province")),
                codigo_postal=_texto(fila.get("postalCode")),
                comunidad=_texto(fila.get("comunidadAutonoma")),
                tipo=_texto(fila.get("type")),
            )
        )
    return resultado[:limite]


async def buscar(q: str, limite: int) -> list[Direccion]:
    clave = (q.casefold(), limite)
    en_cache = cache.obtener(clave)
    if en_cache is not None:
        return en_cache
    try:
        async with crear_cliente() as cliente:
            respuesta = await cliente.get(
                f"{settings.cartociudad_base_url.rstrip('/')}/candidates", params={"q": q, "limit": limite}
            )
        respuesta.raise_for_status()
        resultado = _mapear(respuesta.json(), limite)
    except httpx.TimeoutException as exc:
        logger.warning("Direcciones: CartoCiudad no respondió a tiempo.")
        raise DireccionesNoDisponible("timeout") from exc
    except httpx.HTTPError as exc:
        logger.warning("Direcciones: fallo de red o HTTP con CartoCiudad (%s).", type(exc).__name__)
        raise DireccionesNoDisponible(type(exc).__name__) from exc
    except DireccionesNoDisponible:
        logger.warning("Direcciones: CartoCiudad devolvió una respuesta con formato inesperado.")
        raise
    except ValueError as exc:  # body that is not JSON
        logger.warning("Direcciones: CartoCiudad devolvió un cuerpo que no es JSON.")
        raise DireccionesNoDisponible("json") from exc
    cache.guardar(clave, resultado)
    return resultado


class _OcultarConsultas(logging.Filter):
    """httpx logs every request URL at INFO, and ours carries the typed address: those lines are dropped."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.levelno < logging.WARNING and settings.cartociudad_base_url in record.getMessage())


logging.getLogger("httpx").addFilter(_OcultarConsultas())
