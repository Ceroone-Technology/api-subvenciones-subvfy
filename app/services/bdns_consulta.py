"""Traduce los criterios de una alerta a los parámetros de la BDNS.

**Función pura**: sin I/O ni acceso a base de datos, así que se prueba sin
Postgres y sin red. Quien la llama (el motor) ya trae la alerta y sus filtros
cargados, y le pasa la fecha `desde` del ciclo.

Los nombres y valores de los parámetros están **verificados contra la API
real** (`GET /bdnstrans/api/convocatorias/busqueda`), no sacados de memoria:

- `descripcion` — texto libre.
- `tipoAdministracion` — `C` estado, `A` autonómica, `L` local, `O` otros.
- `fechaDesde` / `fechaHasta` — formato `dd/mm/yyyy` (la respuesta, en cambio,
  devuelve `yyyy-mm-dd`).
- `organos` y `regiones` — ids enteros del catálogo BDNS, **repetibles**: la
  API los acumula (`regiones=6&regiones=13` devuelve la suma de ambas). Por eso
  una alerta con varios órganos y varias regiones cabe en **una sola
  consulta**, sin producto cartesiano ni filtrado en cliente.

`solo_mrr` **no tiene parámetro** en la BDNS. La respuesta trae `mrr` por
convocatoria, así que ese filtro se aplica en el cliente.
"""

from dataclasses import dataclass
from datetime import date

from app.models.alerta import Alerta

# Único sitio donde nuestros valores se traducen a los de la BDNS. Las claves
# son las de NIVELES_ADMINISTRACION en app/models/alerta.py.
NIVEL_A_TIPO_ADMINISTRACION = {"estado": "C", "ccaa": "A", "local": "L", "otros": "O"}

FORMATO_FECHA_BDNS = "%d/%m/%Y"


class CriteriosAlertaInvalidos(ValueError):
    """La alerta no se puede convertir en una consulta útil.

    O no tiene ningún criterio (traería media BDNS), o la combinación es
    imposible (un rango de fechas que ya pasó). El ciclo del motor la registra
    en el log y sigue con las demás.
    """


@dataclass(frozen=True)
class ConsultaBdns:
    """Criterios ya mapeados a lo que entiende la BDNS."""

    descripcion: str | None = None
    tipo_administracion: str | None = None
    organos: tuple[int, ...] = ()
    regiones: tuple[int, ...] = ()
    fecha_desde: date | None = None
    fecha_hasta: date | None = None
    # No viaja a la BDNS: lo aplica el cliente sobre el campo `mrr`.
    solo_mrr: bool = False

    def parametros(self) -> list[tuple[str, str]]:
        """Pares clave/valor para la query, no un dict: `organos` y `regiones`
        se repiten tantas veces como ids haya.

        Los valores van **sin codificar**: de escaparlos se encarga el cliente
        HTTP. El texto del usuario nunca se pega a mano a una URL.
        """
        pares: list[tuple[str, str]] = []
        if self.descripcion:
            pares.append(("descripcion", self.descripcion))
        if self.tipo_administracion:
            pares.append(("tipoAdministracion", self.tipo_administracion))
        pares.extend(("organos", str(id_bdns)) for id_bdns in self.organos)
        pares.extend(("regiones", str(id_bdns)) for id_bdns in self.regiones)
        if self.fecha_desde:
            pares.append(("fechaDesde", self.fecha_desde.strftime(FORMATO_FECHA_BDNS)))
        if self.fecha_hasta:
            pares.append(("fechaHasta", self.fecha_hasta.strftime(FORMATO_FECHA_BDNS)))
        return pares


def construir_consulta(
    alerta: Alerta, organos: list[int], regiones: list[int], *, desde: date
) -> ConsultaBdns:
    """Consulta de una alerta, acotada a lo publicado desde `desde`.

    `desde` llega como parámetro y no se calcula aquí: depende del historial de
    ejecuciones, que es de otra capa.

    Cuando el usuario puso su propia `fecha_desde`, **manda la más
    restrictiva**: lo incremental no vuelve a abrir un rango que él había
    cerrado.
    """
    if not _tiene_algun_criterio(alerta, organos, regiones):
        raise CriteriosAlertaInvalidos(
            f"La alerta {alerta.id} no tiene ningún criterio (texto, nivel, órganos, regiones o fechas): "
            "una consulta así traería la BDNS entera."
        )

    fecha_desde = max(desde, alerta.fecha_desde) if alerta.fecha_desde else desde
    if alerta.fecha_hasta and alerta.fecha_hasta < fecha_desde:
        raise CriteriosAlertaInvalidos(
            f"La alerta {alerta.id} pide hasta {alerta.fecha_hasta}, anterior al inicio efectivo "
            f"{fecha_desde}: no hay nada que consultar."
        )

    return ConsultaBdns(
        descripcion=alerta.texto_busqueda or None,
        tipo_administracion=_tipo_administracion(alerta.nivel_administracion),
        organos=tuple(organos),
        regiones=tuple(regiones),
        fecha_desde=fecha_desde,
        fecha_hasta=alerta.fecha_hasta,
        solo_mrr=alerta.solo_mrr,
    )


def _tiene_algun_criterio(alerta: Alerta, organos: list[int], regiones: list[int]) -> bool:
    """`solo_mrr` no cuenta: no viaja a la BDNS, solo recorta lo que ya se ha
    traído, así que por sí solo no acota la consulta."""
    return any(
        (
            alerta.texto_busqueda,
            alerta.nivel_administracion,
            alerta.fecha_desde,
            alerta.fecha_hasta,
            organos,
            regiones,
        )
    )


def _tipo_administracion(nivel: str | None) -> str | None:
    if nivel is None:
        return None
    try:
        return NIVEL_A_TIPO_ADMINISTRACION[nivel]
    except KeyError as exc:  # pragma: no cover - lo impide el CHECK de la tabla
        raise CriteriosAlertaInvalidos(f"Nivel de administración desconocido: {nivel!r}.") from exc
