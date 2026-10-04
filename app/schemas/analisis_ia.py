"""Formatos del Análisis con IA (Hito 5, H5.2).

Dos familias, y la diferencia importa:

- `Respuesta*`: lo que **rellena la IA**. Su esquema es el de la herramienta
  obligatoria (ver `ClienteIA.generar_estructurado`), así que es a la vez la
  instrucción de forma y la validación de lo que vuelve. Son **planos** a
  propósito: sin modelos anidados, el esquema no lleva referencias internas
  (`$defs`/`$ref`), que es lo que menos riesgo tiene de que Anthropic no lo
  acepte tal cual (sin verificar hasta tener clave, S8).
- `Resultado*`: lo que **devuelve la API** (y guardará H5.3). Es la respuesta
  de la IA más lo que pone el código, que la IA no puede escribir ni quitar:
  el aviso de que no se han leído las bases reguladoras y, en la idoneidad,
  la marca de encaje limitado por un perfil incompleto.

Los topes de tamaño son de seguridad (que una respuesta desbocada no llegue a
la base de datos ni al frontend), no de estilo: la brevedad se pide en las
descripciones. Por eso son holgados, porque una respuesta que se pasa de un
tope se rechaza, y esa llamada ya se ha pagado.

El tipo `riesgos` de `TIPOS_ANALISIS` no tiene formato: existe en la base de
datos, pero no se genera (decisión 6 de H5.2-D, ver S9).
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

ENCAJES = ("alto", "medio", "bajo", "no_encaja")
# Literal[tupla] funciona en runtime pero mypy no lo acepta (ver CLAUDE.md): de ahí el ignore.
Encaje = Literal[ENCAJES]  # type: ignore[valid-type]

Frase = Annotated[str, Field(min_length=1, max_length=500)]


def _frases(descripcion: str, *, maximo: int = 10) -> Any:
    # `default=[]` es seguro en Pydantic, que copia el valor por instancia, y
    # deja `"default": []` en el esquema: la IA ve que puede dejarla vacía.
    return Field(default=[], max_length=maximo, description=descripcion)


# --- Lo que rellena la IA ---------------------------------------------------


class RespuestaResumen(BaseModel):
    resumen: str = Field(
        min_length=1,
        max_length=1500,
        description="Qué financia la convocatoria, quién la convoca y para quién es, en 2 a 4 frases.",
    )
    finalidad: str = Field(min_length=1, max_length=300, description="Para qué es la ayuda, en una frase.")
    a_quien_va: str = Field(
        min_length=1, max_length=500, description="Quién puede ser beneficiario, en una frase."
    )
    puntos_clave: list[Frase] = _frases(
        "Entre 3 y 5 puntos breves con lo más útil para decidir si interesa: importe, plazo, "
        "tipo de beneficiario, ámbito territorial.",
        maximo=8,
    )


class RespuestaRequisitos(BaseModel):
    beneficiarios: list[Frase] = _frases("Quién puede solicitarla y en qué condiciones, según la ficha.")
    ambito_territorial: list[Frase] = _frases("Dónde tiene que estar o actuar el beneficiario.")
    sectores: list[Frase] = _frases("Sectores o actividades a los que se limita, si se limita.")
    plazos: list[Frase] = _frases("Plazo de solicitud y otras fechas que consten en la ficha.")
    cuantia: list[Frase] = _frases("Presupuesto total y cualquier importe o límite que conste en la ficha.")
    documentacion_mencionada: list[Frase] = _frases(
        "Documentación a presentar que la ficha mencione expresamente. Vacía si no consta, que es lo "
        "habitual: suele estar en las bases. No incluyas aquí los documentos publicados de la convocatoria."
    )
    otros_requisitos: list[Frase] = _frases(
        "Otras condiciones relevantes (reglamento de minimis, concesión directa a un beneficiario "
        "concreto, compatibilidad con otras ayudas) y lo que haya que comprobar en las bases."
    )
    informacion_insuficiente: bool = Field(
        description="true si la propia ficha de la BDNS es pobre para conocer los requisitos (sin tipos de "
        "beneficiario, sin plazos, descripción genérica). No lo marques solo porque falten las bases "
        "reguladoras: eso ya se avisa aparte."
    )
    que_falta: list[Frase] = _frases("Si informacion_insuficiente es true, qué datos le faltan a la ficha.")


class RespuestaIdoneidad(BaseModel):
    encaje: Encaje = Field(
        description="alto: cumple lo que exige la ficha y la finalidad encaja con su actividad. "
        "medio: encaja en lo principal, pero hay algo dudoso o que no consta. "
        "bajo: el encaje es parcial o lejano. "
        "no_encaja: un requisito la excluye claramente (otra región, otro tipo de beneficiario, plazo "
        "cerrado, concesión a un beneficiario concreto)."
    )
    motivos_a_favor: list[Frase] = _frases("Hechos concretos de la ficha y del perfil a favor.", maximo=8)
    motivos_en_contra: list[Frase] = _frases("Hechos concretos de la ficha y del perfil en contra.", maximo=8)
    a_verificar: list[Frase] = _frases(
        "Lo que la empresa debe comprobar antes de solicitar, incluidos los datos de su perfil que faltan "
        "y lo que solo estará en las bases."
    )
    explicacion: str = Field(min_length=1, max_length=1000, description="Por qué ese encaje, en 2 o 3 frases.")


# --- Lo que devuelve la API (la respuesta de la IA más lo que pone el código) ---


_DESCRIPCION_AVISO = "Aviso fijo, puesto por el código: el análisis no incluye las bases reguladoras."
_DESCRIPCION_URL_BASES = "Enlace a las bases reguladoras que publica la BDNS, si lo publica."


class ResultadoResumen(RespuestaResumen):
    """Sin aviso de las bases: el resumen no pretende ser completo."""


class ResultadoRequisitos(RespuestaRequisitos):
    aviso_bases: str = Field(description=_DESCRIPCION_AVISO)
    url_bases_reguladoras: str | None = Field(default=None, description=_DESCRIPCION_URL_BASES)


class ResultadoIdoneidad(RespuestaIdoneidad):
    aviso_bases: str = Field(description=_DESCRIPCION_AVISO)
    url_bases_reguladoras: str | None = Field(default=None, description=_DESCRIPCION_URL_BASES)
    encaje_limitado_por_perfil: bool = Field(
        default=False,
        description="true si la IA dio 'alto' y el código lo rebajó a 'medio' porque el perfil no "
        "describe la actividad de la empresa.",
    )
    datos_perfil_que_faltan: list[str] = Field(
        default_factory=list, description="Datos del perfil que importan para la idoneidad y están vacíos."
    )
