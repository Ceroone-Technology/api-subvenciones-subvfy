"""Análisis con IA de una convocatoria (Hito 5, H5.2).

Lanza las llamadas a la IA y completa sus respuestas con lo que pone el
código. **No guarda nada ni sabe de HTTP**: lo usarán los endpoints de H5.3 y
el comando `probar-analisis`.

Lo que pone el código, y no la IA, porque así está garantizado:

- **El aviso de las bases reguladoras** (`AVISO_BASES`), en requisitos e
  idoneidad. La IA no puede escribirlo ni quitarlo: no está en su formato.
- **La regla del perfil incompleto** en la idoneidad:
  - si faltan los cuatro datos del perfil (comunidad, sector, tamaño y
    actividad), no se llama a la IA: `PerfilInsuficiente`, sin coste;
  - si falta la actividad, un "alto" de la IA se rebaja a "medio" y queda
    marcado (`encaje_limitado_por_perfil`): sin saber a qué se dedica la
    empresa no se puede juzgar si la finalidad encaja;
  - si falta otro dato, decide la IA, que sabe cuáles faltan (lo dice el
    prompt) y si hacen falta para esa convocatoria.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.schemas.analisis_ia import ResultadoIdoneidad, ResultadoRequisitos, ResultadoResumen
from app.services.bdns_cliente import DetalleConvocatoriaBdns
from app.services.ia_cliente import ClienteIA, ErrorIA, RespuestaEstructurada, calcular_coste
from app.services.ia_entrada import PerfilEmpresa
from app.services.ia_prompts import (
    TIPO_IDONEIDAD,
    TIPO_REQUISITOS,
    TIPO_RESUMEN,
    FormatoT,
    PeticionIA,
    peticion_idoneidad,
    peticion_requisitos,
    peticion_resumen,
)

ResultadoT = TypeVar("ResultadoT", bound=BaseModel)

AVISO_BASES = (
    "Este análisis se basa solo en los datos publicados en la BDNS y no incluye las bases reguladoras. "
    "Antes de solicitar, revisa las bases: porcentaje de ayuda, gastos subvencionables y documentación exigida."
)


class PerfilInsuficiente(ValueError):
    """El perfil de la empresa no tiene ninguno de los datos que sirven para
    valorar la idoneidad. No se llama a la IA: la respuesta no valdría nada."""

    def __init__(self) -> None:
        super().__init__(
            "El perfil de la empresa no tiene comunidad autónoma, sector, tamaño ni actividad: "
            "no se puede valorar la idoneidad."
        )


@dataclass(frozen=True)
class AnalisisGenerado(Generic[ResultadoT]):
    """Un análisis listo para devolver o guardar (H5.3)."""

    tipo: str
    version_prompt: str
    resultado: ResultadoT
    # El modelo que contestó, que es el que va a `analisis_ia.modelo_ia`.
    modelo: str
    tokens_entrada: int
    tokens_salida: int
    coste_estimado: Decimal | None


async def analizar_resumen(cliente: ClienteIA, ficha: DetalleConvocatoriaBdns) -> AnalisisGenerado[ResultadoResumen]:
    peticion = peticion_resumen(ficha)
    respuesta = await _pedir(cliente, peticion)
    return _generado(peticion, respuesta, ResultadoResumen(**respuesta.datos.model_dump()))


async def analizar_requisitos(
    cliente: ClienteIA, ficha: DetalleConvocatoriaBdns
) -> AnalisisGenerado[ResultadoRequisitos]:
    peticion = peticion_requisitos(ficha)
    respuesta = await _pedir(cliente, peticion)
    resultado = ResultadoRequisitos(
        **respuesta.datos.model_dump(),
        aviso_bases=AVISO_BASES,
        url_bases_reguladoras=ficha.url_bases_reguladoras,
    )
    return _generado(peticion, respuesta, resultado)


async def analizar_idoneidad(
    cliente: ClienteIA, ficha: DetalleConvocatoriaBdns, perfil: PerfilEmpresa, *, hoy: date
) -> AnalisisGenerado[ResultadoIdoneidad]:
    if perfil.insuficiente:
        raise PerfilInsuficiente()

    peticion = peticion_idoneidad(ficha, perfil, hoy=hoy)
    respuesta = await _pedir(cliente, peticion)
    datos = respuesta.datos.model_dump()
    limitado = not perfil.tiene_actividad and datos["encaje"] == "alto"
    if limitado:
        datos["encaje"] = "medio"
    resultado = ResultadoIdoneidad(
        **datos,
        aviso_bases=AVISO_BASES,
        url_bases_reguladoras=ficha.url_bases_reguladoras,
        encaje_limitado_por_perfil=limitado,
        datos_perfil_que_faltan=list(perfil.datos_que_faltan),
    )
    return _generado(peticion, respuesta, resultado)


async def analizar_convocatoria(
    cliente: ClienteIA,
    ficha: DetalleConvocatoriaBdns,
    perfil: PerfilEmpresa | None = None,
    *,
    hoy: date,
) -> dict[str, AnalisisGenerado[Any] | ErrorIA | PerfilInsuficiente]:
    """Los análisis de una convocatoria, en paralelo: se tarda lo que la más
    lenta. Resumen y requisitos siempre; idoneidad solo con perfil.

    **Cada tipo puede fallar por separado**: el resultado de un tipo es su
    análisis o su error, y los demás no se pierden (ya se han pagado). Quien
    llama decide qué hace con un éxito parcial (H5.3). Un error que no sea de
    la IA ni del perfil es un fallo nuestro y se propaga.
    """
    tareas = {
        TIPO_RESUMEN: analizar_resumen(cliente, ficha),
        TIPO_REQUISITOS: analizar_requisitos(cliente, ficha),
    }
    if perfil is not None:
        tareas[TIPO_IDONEIDAD] = analizar_idoneidad(cliente, ficha, perfil, hoy=hoy)

    resultados = await asyncio.gather(*tareas.values(), return_exceptions=True)
    for resultado in resultados:
        if isinstance(resultado, BaseException) and not isinstance(resultado, ErrorIA | PerfilInsuficiente):
            raise resultado
    return dict(zip(tareas, resultados, strict=True))  # type: ignore[arg-type]


async def _pedir(cliente: ClienteIA, peticion: PeticionIA[FormatoT]) -> RespuestaEstructurada[FormatoT]:
    return await cliente.generar_estructurado(
        sistema=peticion.sistema,
        mensaje=peticion.mensaje,
        herramienta=peticion.herramienta,
        descripcion=peticion.descripcion_herramienta,
        formato=peticion.formato,
    )


def _generado(
    peticion: PeticionIA[Any], respuesta: RespuestaEstructurada[Any], resultado: ResultadoT
) -> AnalisisGenerado[ResultadoT]:
    return AnalisisGenerado(
        tipo=peticion.tipo,
        version_prompt=peticion.version,
        resultado=resultado,
        modelo=respuesta.modelo,
        tokens_entrada=respuesta.tokens_entrada,
        tokens_salida=respuesta.tokens_salida,
        coste_estimado=calcular_coste(respuesta.tokens_entrada, respuesta.tokens_salida),
    )
