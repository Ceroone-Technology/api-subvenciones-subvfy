"""Cliente de la BDNS para la sincronización local (`bdns-local-sync`, D9).

Distinto de `ClienteBdns` (el de las alertas, sin reintentos y con orden
descendente), que no se toca. Este está pensado para recorrer la BDNS entera
sin que nos bloquee, y por eso hace cuatro cosas que el otro no:

- **Throttle**: cubo de tokens de 2 a 4 peticiones por segundo, secuencial. La
  BDNS bloquea rápido (una de las sondas devolvió un 200 con una página HTML
  del WAF), así que ir más despacio es parte del contrato.
- **Reintentos** con espera exponencial (`2 s · 2^n` + jitter, tope 60 s) ante
  429, 5xx, timeouts, errores de transporte y **200 que no es JSON**. Se
  respeta `Retry-After` en segundos.
- **Breaker**: tras K llamadas seguidas que fallan (ya sin reintentos) lanza
  `CircuitoAbierto` y el motor termina la ejecución en `failed`, conservando el
  cursor. Una llamada que sale bien reinicia la cuenta.
- **Orden ascendente** por `fechaRecepcion` (D8): los inserts nuevos se añaden
  al final y la página N no se mueve entre ejecuciones.

Solo lectura y sin tocar la base de datos. Reloj, espera y jitter se inyectan
para probarlo sin esperar.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from types import TracebackType
from typing import Any, Literal, Self

import httpx

from app.config import settings
from app.services.bdns_cliente import BdnsNoDisponible, PaginaBdns, _leer_pagina

logger = logging.getLogger(__name__)

RUTA_BUSQUEDA = "/convocatorias/busqueda"
RUTA_DETALLE = "/convocatorias"
RUTA_ORGANOS = "/organos"
RUTA_REGIONES = "/regiones"

FORMATO_FECHA_BDNS = "%d/%m/%Y"
USER_AGENT = "Subvfy-sync/1.0"
VPD = "GE"
TAMANO_PAGINA = 200

RPS_MIN = 2.0
RPS_MAX = 4.0
RPS_POR_DEFECTO = 3.0
INTENTOS_POR_DEFECTO = 4
UMBRAL_BREAKER_POR_DEFECTO = 5

ESPERA_BASE_SEGUNDOS = 2.0
ESPERA_MAX_SEGUNDOS = 60.0
# Fracción del backoff que puede añadir el jitter.
JITTER = 0.25
# Tope a lo que se obedece de un `Retry-After`: no nos dormimos 1 h por un header.
RETRY_AFTER_MAX_SEGUNDOS = 300.0

TIMEOUT_CONEXION = 5.0
TIMEOUT_LECTURA = 30.0

ESTADOS_TRANSITORIOS = frozenset({429, 500, 502, 503, 504})

NivelAdmon = Literal["C", "A", "L", "O"]
Direccion = Literal["asc", "desc"]
OrdenFechas = Literal["asc", "desc", "mixto", "constante"]
DireccionInferida = Literal["asc", "desc", "indeterminado"]


class CircuitoAbierto(RuntimeError):
    """K llamadas seguidas han fallado: la BDNS no está en condiciones.

    A propósito **no** hereda de `BdnsNoDisponible`: el motor captura esa por
    elemento para contarla en `errores` y seguir, y el breaker tiene que
    escapar de ese manejador para terminar la ejecución.
    """


def orden_de_fechas(fechas: Sequence[date]) -> OrdenFechas:
    """Cómo vienen ordenadas las fechas de una página.

    Lo usa la comprobación en vivo de `direccion=asc` (`scripts/check_bdns_asc.py`),
    que decide con una sola petición si la BDNS respeta el orden pedido.
    """
    pares = list(zip(fechas, fechas[1:], strict=False))
    sube = any(b > a for a, b in pares)
    baja = any(b < a for a, b in pares)
    if sube and baja:
        return "mixto"
    if sube:
        return "asc"
    if baja:
        return "desc"
    return "constante"


def inferir_direccion(fechas: Sequence[date], desde: date, hasta: date, *, hay_mas_paginas: bool) -> DireccionInferida:
    """Dirección real de una página pedida con `direccion=asc`, con una sola petición.

    Si las fechas se distinguen entre sí, manda su orden. Si todas son iguales
    (la BDNS registra decenas de convocatorias al día) solo concluye cuando hay
    más páginas y la fecha es un extremo de la ventana: la página 0 de un
    recorrido ascendente empieza en el día más antiguo, y la de uno descendente
    en el más reciente.
    """
    orden = orden_de_fechas(fechas)
    if orden == "asc":
        return "asc"
    if orden == "desc":
        return "desc"
    if orden == "constante" and fechas and hay_mas_paginas:
        if fechas[0] == desde:
            return "asc"
        if fechas[0] == hasta:
            return "desc"
    return "indeterminado"


class _CuboDeTokens:
    """Cubo de capacidad 1: sin ráfagas, una petición cada `1/rps` segundos."""

    def __init__(self, rps: float, reloj: Callable[[], float], dormir: Callable[[float], Awaitable[None]]) -> None:
        self._rps = rps
        self._reloj = reloj
        self._dormir = dormir
        self._tokens = 1.0
        self._ultimo = reloj()

    async def tomar(self) -> None:
        while True:
            ahora = self._reloj()
            self._tokens = min(1.0, self._tokens + (ahora - self._ultimo) * self._rps)
            self._ultimo = ahora
            if self._tokens >= 1.0 - 1e-9:
                self._tokens -= 1.0
                return
            await self._dormir((1.0 - self._tokens) / self._rps)


class ClienteBdnsSync:
    """Se usa como context manager, igual que `ClienteBdns`.

    Acepta un `httpx.AsyncClient` ya construido para que los tests inyecten un
    `MockTransport` y no lleguen nunca a la BDNS real.
    """

    def __init__(
        self,
        cliente: httpx.AsyncClient | None = None,
        *,
        rps: float = RPS_POR_DEFECTO,
        intentos: int = INTENTOS_POR_DEFECTO,
        umbral_breaker: int = UMBRAL_BREAKER_POR_DEFECTO,
        vpd: str = VPD,
        reloj: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        aleatorio: Callable[[], float] = random.random,
    ) -> None:
        self._propio = cliente is None
        self._cliente = cliente or httpx.AsyncClient(
            base_url=settings.bdns_base_url,
            timeout=httpx.Timeout(TIMEOUT_LECTURA, connect=TIMEOUT_CONEXION),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        self._cubo = _CuboDeTokens(max(RPS_MIN, min(RPS_MAX, rps)), reloj, dormir)
        self._intentos = max(1, intentos)
        self._umbral = max(1, umbral_breaker)
        self._vpd = vpd
        self._dormir = dormir
        self._aleatorio = aleatorio
        self._peticiones = 0
        self._fallos_seguidos = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        tipo: type[BaseException] | None,
        valor: BaseException | None,
        traza: TracebackType | None,
    ) -> None:
        await self.cerrar()

    async def cerrar(self) -> None:
        if self._propio:
            await self._cliente.aclose()

    @property
    def peticiones(self) -> int:
        """Peticiones HTTP hechas, reintentos incluidos (`sync_run.peticiones_bdns`)."""
        return self._peticiones

    @property
    def circuito_abierto(self) -> bool:
        return self._fallos_seguidos >= self._umbral

    # --- Endpoints --------------------------------------------------------

    async def listar_pagina(
        self,
        desde: date,
        hasta: date,
        *,
        pagina: int,
        tamano: int = TAMANO_PAGINA,
        direccion: Direccion = "asc",
    ) -> PaginaBdns:
        """Una página del listado, por `fechaRecepcion` ascendente (D8).

        `direccion="desc"` existe como plan B si la comprobación en vivo
        (`scripts/check_bdns_asc.py`) demostrase que la BDNS ignora `asc`.
        """
        parametros = {
            "fechaDesde": desde.strftime(FORMATO_FECHA_BDNS),
            "fechaHasta": hasta.strftime(FORMATO_FECHA_BDNS),
            "page": str(pagina),
            "pageSize": str(tamano),
            "order": "fechaRecepcion",
            "direccion": direccion,
            "vpd": self._vpd,
        }
        cuerpo = await self._pedir(RUTA_BUSQUEDA, parametros)
        try:
            return _leer_pagina(cuerpo, pagina)
        except BdnsNoDisponible:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BdnsNoDisponible(f"La BDNS devolvió una página con forma inesperada: {exc!r}.") from exc

    async def detalle(self, codigo_bdns: str) -> dict[str, Any] | None:
        """Detalle de una convocatoria, o `None` si la BDNS dice que no existe.

        404, 204 y un JSON `null` o `{}` significan "no existe" (retirada). Un
        200 con el cuerpo vacío **no**: no es JSON, y es lo que devuelve el WAF
        a veces; marcar como retirada por eso sería borrar datos por un bloqueo.
        """
        cuerpo = await self._pedir(RUTA_DETALLE, {"numConv": codigo_bdns, "vpd": self._vpd}, vacio_si_no_existe=True)
        if not cuerpo:
            return None
        if not isinstance(cuerpo, dict):
            raise BdnsNoDisponible("La BDNS devolvió un detalle que no es un objeto JSON.")
        return cuerpo

    async def organos(self, nivel: NivelAdmon) -> list[dict[str, Any]]:
        return await self._catalogo(RUTA_ORGANOS, {"vpd": self._vpd, "idAdmon": nivel})

    async def regiones(self) -> list[dict[str, Any]]:
        return await self._catalogo(RUTA_REGIONES, {"vpd": self._vpd})

    async def _catalogo(self, ruta: str, parametros: dict[str, str]) -> list[dict[str, Any]]:
        cuerpo = await self._pedir(ruta, parametros)
        if not isinstance(cuerpo, list):
            raise BdnsNoDisponible(f"La BDNS devolvió un catálogo que no es una lista ({ruta}).")
        return cuerpo

    # --- Núcleo: throttle, reintentos, breaker -----------------------------

    async def _pedir(self, ruta: str, parametros: dict[str, str], *, vacio_si_no_existe: bool = False) -> Any:
        if self.circuito_abierto:
            raise CircuitoAbierto(f"Circuito abierto tras {self._umbral} fallos seguidos de la BDNS.")

        error: BdnsNoDisponible | None = None
        for intento in range(1, self._intentos + 1):
            await self._cubo.tomar()
            self._peticiones += 1
            reintentar_tras: float | None = None
            try:
                respuesta = await self._cliente.get(ruta, params=parametros)
            except httpx.HTTPError as exc:
                # Timeouts y errores de transporte: para quien llama es lo mismo.
                error = BdnsNoDisponible(f"No se pudo consultar la BDNS: {exc!r}.")
                transitorio = True
            else:
                estado = respuesta.status_code
                if vacio_si_no_existe and estado in (204, 404):
                    self._fallos_seguidos = 0
                    return None
                if estado in ESTADOS_TRANSITORIOS:
                    error = BdnsNoDisponible(f"La BDNS respondió {estado}.", status=estado)
                    reintentar_tras = _leer_retry_after(respuesta)
                    transitorio = True
                elif estado >= 400:
                    error = BdnsNoDisponible(f"La BDNS respondió {estado}.", status=estado)
                    transitorio = False
                else:
                    try:
                        cuerpo = _leer_json(respuesta)
                    except ValueError as exc:
                        # El WAF contesta 200 con HTML: se trata como un fallo transitorio.
                        error = BdnsNoDisponible(f"La BDNS devolvió algo que no es JSON: {exc}.", status=estado)
                        transitorio = True
                    else:
                        self._fallos_seguidos = 0
                        return cuerpo

            if not transitorio or intento == self._intentos:
                break
            await self._dormir(self._espera(intento, reintentar_tras))

        self._fallos_seguidos += 1
        assert error is not None
        if self.circuito_abierto:
            logger.error("BDNS: circuito abierto tras %d fallos seguidos.", self._fallos_seguidos)
            raise CircuitoAbierto(
                f"Circuito abierto tras {self._fallos_seguidos} fallos seguidos de la BDNS."
            ) from error
        raise error

    def _espera(self, intento: int, retry_after: float | None) -> float:
        base = ESPERA_BASE_SEGUNDOS * 2 ** (intento - 1)
        backoff = min(ESPERA_MAX_SEGUNDOS, base * (1 + JITTER * self._aleatorio()))
        if retry_after is None:
            return backoff
        return min(RETRY_AFTER_MAX_SEGUNDOS, max(backoff, retry_after))


def _leer_json(respuesta: httpx.Response) -> Any:
    if "html" in respuesta.headers.get("content-type", "").lower():
        raise ValueError("la respuesta es HTML (página de bloqueo)")
    return respuesta.json()


def _leer_retry_after(respuesta: httpx.Response) -> float | None:
    """`Retry-After` en segundos. La variante con fecha HTTP se ignora y se usa el backoff."""
    crudo = respuesta.headers.get("retry-after")
    if crudo is None:
        return None
    try:
        segundos = float(crudo)
    except ValueError:
        return None
    return segundos if segundos >= 0 else None
