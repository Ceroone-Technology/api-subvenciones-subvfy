"""Cliente HTTP mínimo de la BDNS.

Solo lectura y **sin tocar la base de datos**: devuelve los resultados a quien
llama y ahí acaba. Dos consultas: la búsqueda, que usa el motor de alertas
(guardar y deduplicar es la tarea 3), y el detalle de una convocatoria, que es
lo que se manda a la IA en el análisis (Hito 5).

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
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any, Self

import httpx

from app.config import settings
from app.services.bdns_consulta import ConsultaBdns

logger = logging.getLogger(__name__)

RUTA_BUSQUEDA = "/convocatorias/busqueda"
RUTA_DETALLE = "/convocatorias"
# El tamaño de `convocatoria.codigo_bdns`.
MAX_LONGITUD_CODIGO = 30


class BdnsNoDisponible(RuntimeError):
    """La BDNS no contestó, o contestó con un error. `status` cuando lo haya."""

    def __init__(self, mensaje: str, *, status: int | None = None) -> None:
        super().__init__(mensaje)
        self.status = status


class ConvocatoriaNoEncontrada(LookupError):
    """La BDNS contestó bien, pero no tiene ninguna convocatoria con ese código."""

    def __init__(self, codigo: str) -> None:
        super().__init__(f"La BDNS no tiene ninguna convocatoria con el código {codigo}.")
        self.codigo = codigo


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
class DocumentoBdns:
    descripcion: str | None
    nombre_fichero: str | None


@dataclass(frozen=True)
class DetalleConvocatoriaBdns:
    """La ficha completa de una convocatoria, con nuestros nombres.

    Solo los campos que sirven para analizarla o para guardarla en la caché.
    Se quedan fuera, a propósito, la descripción en lengua cooficial
    (`descripcionLeng`), el aviso legal (`advertencia`), los anuncios y los
    identificadores internos de la BDNS. Las listas de la BDNS llegan como
    objetos con `descripcion`; aquí quedan como textos.
    """

    id_bdns: int
    codigo_bdns: str | None
    titulo: str | None
    fecha_registro: date | None
    nivel1: str | None
    nivel2: str | None
    nivel3: str | None
    financiada_mrr: bool
    sede_electronica: str | None
    tipo_convocatoria: str | None
    finalidad: str | None
    instrumentos: tuple[str, ...]
    tipos_beneficiario: tuple[str, ...]
    sectores: tuple[str, ...]
    regiones: tuple[str, ...]
    presupuesto_total: Decimal | None
    # Ojo: el `abierto` de la BDNS **no** dice si el plazo está abierto hoy.
    # En 40 fichas consultadas el 2026-10-04, una con plazo del 01/10 al 01/11
    # salía `false`, y los únicos `true` eran concesiones sin fechas. No se
    # usa para decidir nada ni se manda a la IA.
    abierta: bool | None
    # Unas convocatorias traen fechas de solicitud y otras solo un texto
    # ("Día siguiente a la publicación en DOE"): se guardan las dos cosas.
    fecha_inicio_solicitud: date | None
    fecha_fin_solicitud: date | None
    texto_inicio_solicitud: str | None
    texto_fin_solicitud: str | None
    bases_reguladoras: str | None
    url_bases_reguladoras: str | None
    reglamento: str | None
    ayuda_estado: str | None
    fondos: tuple[str, ...]
    objetivos: tuple[str, ...]
    documentos: tuple[DocumentoBdns, ...]


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
        # Como tupla y no como lista: `list` es invariante y mypy rechaza
        # list[tuple[str, str]] donde httpx admite valores de más tipos.
        respuesta = await self._pedir(RUTA_BUSQUEDA, tuple(parametros))
        return _leer_pagina(_json(respuesta), pagina)

    async def obtener_detalle(self, codigo: str) -> DetalleConvocatoriaBdns:
        """La ficha completa de una convocatoria por su código BDNS.

        Un código que no existe no es un error de la BDNS: contesta 204 sin
        cuerpo, y aquí sale como `ConvocatoriaNoEncontrada`. El código se
        valida antes de llamar porque, vacío, el cortafuegos de la BDNS no
        contesta con un error sino con una página HTML de "Acceso denegado".
        """
        codigo = codigo.strip()
        if not codigo or len(codigo) > MAX_LONGITUD_CODIGO:
            raise ValueError(f"El código BDNS debe tener entre 1 y {MAX_LONGITUD_CODIGO} caracteres.")

        respuesta = await self._pedir(RUTA_DETALLE, (("numConv", codigo),))
        if respuesta.status_code == httpx.codes.NO_CONTENT or not respuesta.content.strip():
            raise ConvocatoriaNoEncontrada(codigo)
        return _leer_detalle(_json(respuesta))

    async def _pedir(self, ruta: str, parametros: tuple[tuple[str, str], ...]) -> httpx.Response:
        try:
            respuesta = await self._cliente.get(ruta, params=parametros)
            respuesta.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BdnsNoDisponible(
                f"La BDNS respondió {exc.response.status_code}.", status=exc.response.status_code
            ) from exc
        except httpx.HTTPError as exc:
            # Cubre timeouts y errores de transporte: para quien llama es lo
            # mismo, la BDNS no está disponible ahora.
            raise BdnsNoDisponible(f"No se pudo consultar la BDNS: {exc!r}.") from exc
        return respuesta

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


def _json(respuesta: httpx.Response) -> Any:
    try:
        return respuesta.json()
    except ValueError as exc:
        raise BdnsNoDisponible(f"La BDNS devolvió algo que no es JSON: {exc}.") from exc


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


def _leer_detalle(cuerpo: Any) -> DetalleConvocatoriaBdns:
    if not isinstance(cuerpo, dict):
        raise BdnsNoDisponible("La BDNS devolvió un detalle inesperado (no es un objeto JSON).")
    try:
        id_bdns = int(cuerpo["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BdnsNoDisponible("La BDNS devolvió un detalle sin identificador.") from exc

    organo = cuerpo.get("organo")
    if not isinstance(organo, dict):
        organo = {}
    reglamento = cuerpo.get("reglamento")
    return DetalleConvocatoriaBdns(
        id_bdns=id_bdns,
        # En el detalle se llama `codigoBDNS`; en la búsqueda, `numeroConvocatoria`.
        codigo_bdns=_texto_limpio(cuerpo.get("codigoBDNS")),
        titulo=_texto_limpio(cuerpo.get("descripcion")),
        fecha_registro=_fecha(cuerpo.get("fechaRecepcion")),
        nivel1=_texto_limpio(organo.get("nivel1")),
        nivel2=_texto_limpio(organo.get("nivel2")),
        nivel3=_texto_limpio(organo.get("nivel3")),
        financiada_mrr=bool(cuerpo.get("mrr", False)),
        sede_electronica=_texto_limpio(cuerpo.get("sedeElectronica")),
        tipo_convocatoria=_texto_limpio(cuerpo.get("tipoConvocatoria")),
        finalidad=_texto_limpio(cuerpo.get("descripcionFinalidad")),
        instrumentos=_descripciones(cuerpo.get("instrumentos")),
        tipos_beneficiario=_descripciones(cuerpo.get("tiposBeneficiarios")),
        sectores=_descripciones(cuerpo.get("sectores")),
        regiones=_descripciones(cuerpo.get("regiones")),
        presupuesto_total=_importe(cuerpo.get("presupuestoTotal")),
        abierta=cuerpo["abierto"] if isinstance(cuerpo.get("abierto"), bool) else None,
        fecha_inicio_solicitud=_fecha(cuerpo.get("fechaInicioSolicitud")),
        fecha_fin_solicitud=_fecha(cuerpo.get("fechaFinSolicitud")),
        texto_inicio_solicitud=_texto_limpio(cuerpo.get("textInicio")),
        texto_fin_solicitud=_texto_limpio(cuerpo.get("textFin")),
        bases_reguladoras=_texto_limpio(cuerpo.get("descripcionBasesReguladoras")),
        url_bases_reguladoras=_texto_limpio(cuerpo.get("urlBasesReguladoras")),
        reglamento=_texto_limpio(reglamento.get("descripcion")) if isinstance(reglamento, dict) else None,
        ayuda_estado=_texto_limpio(cuerpo.get("ayudaEstado")),
        fondos=_descripciones(cuerpo.get("fondos")),
        objetivos=_descripciones(cuerpo.get("objetivos")),
        documentos=tuple(
            DocumentoBdns(
                descripcion=_texto_limpio(documento.get("descripcion")),
                nombre_fichero=_texto_limpio(documento.get("nombreFic")),
            )
            for documento in cuerpo.get("documentos") or []
            if isinstance(documento, dict)
        ),
    )


def _texto(valor: Any) -> str | None:
    return str(valor) if valor is not None else None


def _texto_limpio(valor: Any) -> str | None:
    """Como `_texto`, pero sin espacios sobrantes y con la cadena vacía como
    None: la BDNS devuelve textos como "SUBVENCIÓN ... CONTRAPRESTACIÓN "."""
    texto = str(valor).strip() if valor is not None else ""
    return texto or None


def _descripciones(valor: Any) -> tuple[str, ...]:
    """`[{"descripcion": "..."}, ...]` → `("...", ...)`, sin vacíos.

    Acepta también textos sueltos, por si algún campo de la BDNS que hoy llega
    vacío (`fondos`, `objetivos`) viene en otra forma.
    """
    if not isinstance(valor, list):
        return ()
    textos = (_texto_limpio(item.get("descripcion") if isinstance(item, dict) else item) for item in valor)
    return tuple(texto for texto in textos if texto)


def _importe(valor: Any) -> Decimal | None:
    """Un importe ilegible no invalida la convocatoria entera."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        importe = Decimal(str(valor))
    except InvalidOperation:
        importe = None
    if importe is None or not importe.is_finite():
        logger.warning("BDNS: importe no interpretable %r.", valor)
        return None
    return importe


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
