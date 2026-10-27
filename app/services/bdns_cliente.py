"""Cliente HTTP mínimo de la BDNS.

Solo lectura y **sin tocar la base de datos**: devuelve los resultados al
motor y ahí acaba. Guardarlos y deduplicarlos es la tarea 3.

**Sin reintentos**: cualquier problema de red o HTTP sale como
`BdnsNoDisponible`, y el aislamiento de fallos del ciclo (ver
`app/services/motor_alertas.py`) la registra y sigue con las demás alertas.
Reintentar aquí, en silencio, solo multiplicaría la carga sobre una API
pública cuando ya va mal.

La respuesta de la BDNS es una página estilo Spring (`content`, `totalPages`,
`number`, `last`, ...). Se parsea a dataclasses para no pasear diccionarios
crudos por el resto del código, y de forma defensiva: un campo que falte queda
en `None` en vez de reventar con `KeyError`.
"""

import logging
from dataclasses import dataclass
from datetime import date
from types import TracebackType
from typing import Any, Self

import httpx

from app.config import settings
from app.services.bdns_consulta import ConsultaBdns

logger = logging.getLogger(__name__)

RUTA_BUSQUEDA = "/convocatorias/busqueda"


class BdnsNoDisponible(RuntimeError):
    """La BDNS no contestó, o contestó con un error. `status` cuando lo haya."""

    def __init__(self, mensaje: str, *, status: int | None = None) -> None:
        super().__init__(mensaje)
        self.status = status


@dataclass(frozen=True)
class ConvocatoriaBdns:
    """Una convocatoria tal como la devuelve la BDNS, con nuestros nombres."""

    id_bdns: int
    codigo_bdns: str | None
    titulo: str | None
    fecha_registro: date | None
    nivel1: str | None
    nivel2: str | None
    nivel3: str | None
    financiada_mrr: bool


@dataclass(frozen=True)
class PaginaBdns:
    convocatorias: list[ConvocatoriaBdns]
    pagina: int
    total_paginas: int
    total: int
    ultima: bool


class ClienteBdns:
    """Se usa como context manager para cerrar las conexiones al terminar.

    Acepta un `httpx.AsyncClient` ya construido, que es lo que permite a los
    tests inyectar un `MockTransport` y no llamar nunca a la BDNS real.
    """

    def __init__(self, cliente: httpx.AsyncClient | None = None) -> None:
        self._propio = cliente is None
        self._cliente = cliente or httpx.AsyncClient(
            base_url=settings.bdns_base_url,
            # Timeout explícito: sin él, httpx espera para siempre y un ciclo
            # del motor se quedaría colgado.
            timeout=httpx.Timeout(settings.bdns_timeout_segundos),
            headers={"Accept": "application/json"},
        )

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

    async def buscar_pagina(self, consulta: ConsultaBdns, *, pagina: int = 0) -> PaginaBdns:
        """Una página de resultados. `pagina` empieza en 0, como la BDNS."""
        parametros = [
            *consulta.parametros(),
            ("page", str(pagina)),
            ("pageSize", str(settings.bdns_tamano_pagina)),
            # Lo más reciente primero: si se corta por el tope de páginas, al
            # menos se corta por lo más viejo.
            ("order", "fechaRecepcion"),
            ("direccion", "desc"),
        ]
        try:
            # Como tupla y no como lista: `list` es invariante y mypy rechaza
            # list[tuple[str, str]] donde httpx admite valores de más tipos.
            respuesta = await self._cliente.get(RUTA_BUSQUEDA, params=tuple(parametros))
            respuesta.raise_for_status()
            cuerpo = respuesta.json()
        except httpx.HTTPStatusError as exc:
            raise BdnsNoDisponible(
                f"La BDNS respondió {exc.response.status_code}.", status=exc.response.status_code
            ) from exc
        except httpx.HTTPError as exc:
            # Cubre timeouts y errores de transporte: para quien llama es lo
            # mismo, la BDNS no está disponible ahora.
            raise BdnsNoDisponible(f"No se pudo consultar la BDNS: {exc!r}.") from exc
        except ValueError as exc:
            raise BdnsNoDisponible(f"La BDNS devolvió algo que no es JSON: {exc}.") from exc

        return _leer_pagina(cuerpo, pagina)

    async def buscar_todo(self, consulta: ConsultaBdns) -> list[ConvocatoriaBdns]:
        """Recorre las páginas hasta la última o hasta el tope configurado.

        El tope existe para que una consulta demasiado abierta no se lleve por
        delante el ciclo entero; si se alcanza, queda avisado en el log.
        """
        encontradas: list[ConvocatoriaBdns] = []
        for pagina in range(settings.bdns_max_paginas):
            resultado = await self.buscar_pagina(consulta, pagina=pagina)
            encontradas.extend(resultado.convocatorias)
            if resultado.ultima or not resultado.convocatorias:
                break
        else:
            logger.warning(
                "BDNS: alcanzado el tope de %d páginas; puede haber más resultados sin traer.",
                settings.bdns_max_paginas,
            )

        if consulta.solo_mrr:
            # La BDNS no tiene parámetro para esto: se filtra con el campo que
            # trae cada convocatoria.
            encontradas = [convocatoria for convocatoria in encontradas if convocatoria.financiada_mrr]
        return encontradas


def _leer_pagina(cuerpo: Any, pagina_pedida: int) -> PaginaBdns:
    if not isinstance(cuerpo, dict):
        raise BdnsNoDisponible("La BDNS devolvió un cuerpo inesperado (no es un objeto JSON).")
    contenido = cuerpo.get("content") or []
    if not isinstance(contenido, list):
        raise BdnsNoDisponible("La BDNS devolvió un 'content' inesperado.")

    convocatorias = [_leer_convocatoria(fila) for fila in contenido if isinstance(fila, dict)]
    return PaginaBdns(
        convocatorias=convocatorias,
        pagina=int(cuerpo.get("number", pagina_pedida) or 0),
        total_paginas=int(cuerpo.get("totalPages", 0) or 0),
        total=int(cuerpo.get("totalElements", len(convocatorias)) or 0),
        ultima=bool(cuerpo.get("last", False)),
    )


def _leer_convocatoria(fila: dict[str, Any]) -> ConvocatoriaBdns:
    return ConvocatoriaBdns(
        id_bdns=int(fila["id"]),
        # `numeroConvocatoria` es el código con el que el frontend y nuestra
        # caché identifican una convocatoria.
        codigo_bdns=_texto(fila.get("numeroConvocatoria")),
        titulo=_texto(fila.get("descripcion")),
        fecha_registro=_fecha(fila.get("fechaRecepcion")),
        nivel1=_texto(fila.get("nivel1")),
        nivel2=_texto(fila.get("nivel2")),
        nivel3=_texto(fila.get("nivel3")),
        financiada_mrr=bool(fila.get("mrr", False)),
    )


def _texto(valor: Any) -> str | None:
    return str(valor) if valor is not None else None


def _fecha(valor: Any) -> date | None:
    """La BDNS devuelve `yyyy-mm-dd` aquí, aunque los filtros van en
    `dd/mm/yyyy`. Una fecha ilegible no invalida la convocatoria entera."""
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        logger.warning("BDNS: fecha no interpretable %r.", valor)
        return None
