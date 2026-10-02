"""Cliente mínimo de Anthropic para el Análisis con IA (Hito 5, tarea H5.1).

Un envoltorio fino sobre `AsyncAnthropic` que hace tres cosas y ninguna más:
manda un prompt, devuelve el texto con los tokens consumidos y traduce los
fallos del SDK a errores propios. No sabe de convocatorias, de empresas ni
de la base de datos: el prompt lo construye H5.2 y lo que se guarda, H5.3.

**Errores propios, en tres familias**, porque quien llama tiene que decidir
cosas distintas con cada una:

- `IANoConfigurada`: no hay clave, Anthropic la rechaza (401/403) o el modelo
  configurado no existe (404). Reintentar no sirve; hay que tocar el `.env`.
- `IANoDisponible`: timeout, red, 429, 5xx o 529 (sobrecarga). Es transitorio:
  el usuario puede volver a intentarlo más tarde.
- `IARespuestaInvalida`: Anthropic contestó, pero la respuesta no vale (vacía
  o cortada por `max_tokens`) o rechazó la petición por mal formada (400/422).
  Es un fallo nuestro, no del usuario.

**Ni la clave ni el prompt salen nunca en un mensaje de error ni en el log.**
Por eso no se usa `str(exc)` del SDK: el prompt lleva el perfil de la empresa
y la ficha de la convocatoria. Solo se registran modelo, tokens, duración y,
en los fallos, el status HTTP y el `request_id` (lo que pide el soporte de
Anthropic para investigar una llamada).

**Reintentos: los del SDK y pocos** (`ANTHROPIC_MAX_REINTENTOS`, 1 por
defecto). Al contrario que la BDNS, que se consulta desde un proceso en
segundo plano y no reintenta, aquí hay un usuario esperando y un 429 o un 529
de Anthropic suelen ser de un instante. El SDK ya aplica backoff y respeta
`retry-after`, así que no se reimplementa.
"""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from types import TracebackType
from typing import Self

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

from app.config import settings

logger = logging.getLogger(__name__)

# `analisis_ia.coste_estimado` es Numeric(10, 4).
_PRECISION_COSTE = Decimal("0.0001")
_TOKENS_POR_MILLON = Decimal(1_000_000)


class ErrorIA(RuntimeError):
    """Base de los errores del cliente. `status` y `request_id` cuando los haya."""

    def __init__(self, mensaje: str, *, status: int | None = None, request_id: str | None = None) -> None:
        super().__init__(mensaje)
        self.status = status
        self.request_id = request_id


class IANoConfigurada(ErrorIA):
    """Falta la clave, es inválida o el modelo configurado no existe."""


class IANoDisponible(ErrorIA):
    """Anthropic no está disponible ahora (timeout, red, 429, 5xx)."""


class IARespuestaInvalida(ErrorIA):
    """La respuesta no se puede usar, o la petición estaba mal formada."""


@dataclass(frozen=True)
class RespuestaIA:
    texto: str
    # El modelo que contestó según Anthropic, no el que se pidió: es el que
    # se guarda en `analisis_ia.modelo_ia`.
    modelo: str
    tokens_entrada: int
    tokens_salida: int


class ClienteIA:
    """Se usa como context manager para cerrar las conexiones al terminar.

    Acepta un `AsyncAnthropic` ya construido, que es lo que permite a los tests
    inyectar uno con un `httpx.MockTransport` y no llamar nunca a Anthropic.
    Sin cliente inyectado y sin clave, **no falla al construirse** sino al
    generar: así un endpoint que solo lee análisis ya guardados funciona
    aunque la IA no esté configurada.
    """

    def __init__(self, cliente: AsyncAnthropic | None = None) -> None:
        self._propio = cliente is None
        if cliente is None and settings.anthropic_api_key:
            cliente = AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.anthropic_timeout_segundos,
                max_retries=settings.anthropic_max_reintentos,
            )
        self._cliente = cliente

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
        if self._propio and self._cliente is not None:
            await self._cliente.close()

    async def generar(self, *, sistema: str, mensaje: str, max_tokens: int | None = None) -> RespuestaIA:
        """Una llamada a la Messages API con un único mensaje de usuario."""
        if self._cliente is None:
            raise IANoConfigurada("La IA no está configurada: falta ANTHROPIC_API_KEY.")

        inicio = time.monotonic()
        try:
            respuesta = await self._cliente.messages.create(
                model=settings.anthropic_model,
                max_tokens=max_tokens or settings.anthropic_max_tokens,
                system=sistema,
                messages=[{"role": "user", "content": mensaje}],
            )
        except anthropic.APIStatusError as exc:
            raise _traducir_status(exc) from exc
        except anthropic.APITimeoutError as exc:
            # Va antes que APIConnectionError porque hereda de ella.
            logger.warning("Anthropic: timeout tras %.1f s.", time.monotonic() - inicio)
            raise IANoDisponible("Anthropic no respondió a tiempo.") from exc
        except anthropic.APIConnectionError as exc:
            logger.warning("Anthropic: error de conexión (%s).", type(exc).__name__)
            raise IANoDisponible("No se pudo conectar con Anthropic.") from exc

        duracion = time.monotonic() - inicio
        texto = "".join(bloque.text for bloque in respuesta.content if isinstance(bloque, TextBlock)).strip()
        logger.info(
            "Anthropic: %s, %d tokens de entrada, %d de salida, %.1f s, stop_reason=%s.",
            respuesta.model,
            respuesta.usage.input_tokens,
            respuesta.usage.output_tokens,
            duracion,
            respuesta.stop_reason,
        )

        if respuesta.stop_reason == "max_tokens":
            # Una respuesta cortada no es "casi buena": el análisis espera un
            # JSON completo, y medio JSON no se puede leer.
            raise IARespuestaInvalida(
                "La respuesta de la IA se cortó por el límite de tokens (ANTHROPIC_MAX_TOKENS)."
            )
        if not texto:
            raise IARespuestaInvalida("La IA devolvió una respuesta vacía.")

        return RespuestaIA(
            texto=texto,
            modelo=respuesta.model,
            tokens_entrada=respuesta.usage.input_tokens,
            tokens_salida=respuesta.usage.output_tokens,
        )


def _traducir_status(exc: anthropic.APIStatusError) -> ErrorIA:
    """Error HTTP de Anthropic → error propio, sin arrastrar el cuerpo."""
    status = exc.status_code
    request_id = exc.response.headers.get("request-id")
    logger.warning("Anthropic respondió %d (request_id=%s).", status, request_id)

    tipo: type[ErrorIA]
    if status in (401, 403):
        tipo, mensaje = IANoConfigurada, "Anthropic rechazó las credenciales configuradas."
    elif status == 404:
        tipo, mensaje = IANoConfigurada, "El modelo configurado (ANTHROPIC_MODEL) no está disponible."
    elif status in (400, 413, 422):
        tipo, mensaje = IARespuestaInvalida, f"Anthropic rechazó la petición ({status})."
    else:
        # 429, 5xx, 529 y cualquier otro: transitorio para quien llama.
        tipo, mensaje = IANoDisponible, f"Anthropic no está disponible ahora ({status})."
    return tipo(mensaje, status=status, request_id=request_id)


def calcular_coste(tokens_entrada: int, tokens_salida: int) -> Decimal | None:
    """Coste en USD con los precios de la configuración, o None sin ellos.

    Hacen falta los dos precios: un coste con solo la mitad parecería real y
    se quedaría corto.
    """
    entrada = settings.anthropic_precio_entrada_millon
    salida = settings.anthropic_precio_salida_millon
    if entrada is None or salida is None:
        return None
    coste = (Decimal(tokens_entrada) * entrada + Decimal(tokens_salida) * salida) / _TOKENS_POR_MILLON
    return coste.quantize(_PRECISION_COSTE, rounding=ROUND_HALF_UP)


async def obtener_cliente_ia() -> AsyncIterator[ClienteIA]:
    """Dependencia de FastAPI: un cliente por petición, cerrado al terminar.

    Los tests la sustituyen con `app.dependency_overrides`.
    """
    async with ClienteIA() as cliente:
        yield cliente
