"""Datos de entrada del Análisis con IA (Hito 5, H5.2).

Convierte la ficha de la BDNS y el perfil de la empresa en el texto que se
manda a la IA. Tres reglas que no son accidentales:

- **Nada que identifique a la empresa.** Del perfil solo salen sector,
  tamaño, comunidad autónoma, descripción, palabras clave y el tipo de
  persona. La razón social y el NIF no salen nunca: el NIF de un autónomo es
  su DNI, y para valorar la idoneidad no aporta nada. Del NIF solo se deriva
  si es persona física o jurídica, que sí es una señal (el tipo de
  beneficiario), y que no identifica a nadie.
- **Todo con tope de tamaño.** Unas bases descritas en varios párrafos o un
  perfil con mucho texto libre no deben disparar el coste de una llamada.
  Cada campo se recorta y cada lista se corta con un "(y N más)". Las listas
  que dicen **quién puede pedir la ayuda** (tipos de beneficiario, sectores y
  regiones) tienen un tope mucho más alto: cortarlas escondería justo la señal
  que necesita la idoneidad (una ficha real trae 21 sectores, y el que quedara
  fuera sería el de la empresa).
- **Datos, no instrucciones.** El texto va entre etiquetas (`<convocatoria>`,
  `<perfil_empresa>`) y el prompt dice que lo de dentro son datos. Por eso se
  cambian los `<` y `>` del texto externo: así nadie puede cerrar una
  etiqueta desde la descripción de la empresa o de la convocatoria. También
  los caracteres que se les parecen (`＜`, `〈`...), que un modelo podría leer
  igual.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Empresa, EmpresaPalabraClave
from app.services.bdns_cliente import DetalleConvocatoriaBdns

# Topes de tamaño. Son de caracteres, no de tokens: en español, un token son
# unos 4 caracteres, así que una ficha completa ronda los 1.000-2.000 tokens.
MAX_CARACTERES_CAMPO = 1500
MAX_CARACTERES_ELEMENTO = 200
MAX_ELEMENTOS_LISTA = 15
MAX_ELEMENTOS_CRITERIO = 40
MAX_DOCUMENTOS = 10
MAX_CARACTERES_DESCRIPCION_EMPRESA = 2000
MAX_PALABRAS_CLAVE = 30

NO_CONSTA = "no consta"

PERSONA_FISICA = "persona física"
PERSONA_JURIDICA = "persona jurídica"
ENTIDAD_SIN_PERSONALIDAD = "entidad sin personalidad jurídica"
TIPOS_PERSONA = (PERSONA_FISICA, PERSONA_JURIDICA, ENTIDAD_SIN_PERSONALIDAD, NO_CONSTA)

# Formatos del NIF español. Solo se mira la forma, no la letra de control:
# para saber si es persona física o jurídica no hace falta, y un NIF con la
# letra mal escrita sigue diciendo lo mismo.
_DNI = re.compile(r"\d{8}[A-Z]")
# X, Y, Z: NIE de extranjeros. K, L, M: NIF de personas físicas sin DNI.
_NIF_PERSONA_FISICA = re.compile(r"[XYZKLM]\d{7}[A-Z]")
# E: comunidades de bienes y herencias yacentes; H: comunidades de
# propietarios; U: uniones temporales de empresas. Pueden ser beneficiarias
# de subvenciones sin tener personalidad jurídica (art. 11.3 de la Ley
# General de Subvenciones), así que no se las mete con las sociedades.
_NIF_SIN_PERSONALIDAD = re.compile(r"[EHU]\d{7}[0-9A-J]")
# La J (sociedades civiles) no está en ninguno de los dos y sale como "no
# consta": una sociedad civil puede tener personalidad jurídica o no (art. 1669
# del Código Civil), y la letra no lo dice. Decidido por Selena el 2026-10-04,
# pendiente de comentarlo con el líder.
_NIF_PERSONA_JURIDICA = re.compile(r"[ABCDFGNPQRSVW]\d{7}[0-9A-J]")

TAMANOS = {
    "micro": "microempresa",
    "pequena": "pequeña empresa",
    "mediana": "mediana empresa",
    "grande": "gran empresa",
}

# Los cuatro datos del perfil que importan para la idoneidad. El tipo de
# persona no está porque casi siempre consta (sale del NIF).
DATO_CCAA = "comunidad autónoma"
DATO_SECTOR = "sector"
DATO_TAMANO = "tamaño"
DATO_ACTIVIDAD = "actividad (descripción o palabras clave)"


def tipo_persona(nif: str | None) -> str:
    """Persona física, jurídica o entidad sin personalidad, según la forma del NIF.

    Con un NIF extranjero, mal formado o vacío, o de una sociedad civil (J),
    "no consta": mejor no saberlo que adivinarlo.
    """
    if not nif:
        return NO_CONSTA
    normalizado = re.sub(r"[\s.\-]", "", nif).upper()
    # El NIF-IVA intracomunitario es el mismo NIF con "ES" delante.
    if len(normalizado) == 11 and normalizado.startswith("ES"):
        normalizado = normalizado[2:]
    if _DNI.fullmatch(normalizado) or _NIF_PERSONA_FISICA.fullmatch(normalizado):
        return PERSONA_FISICA
    if _NIF_SIN_PERSONALIDAD.fullmatch(normalizado):
        return ENTIDAD_SIN_PERSONALIDAD
    if _NIF_PERSONA_JURIDICA.fullmatch(normalizado):
        return PERSONA_JURIDICA
    return NO_CONSTA


@dataclass(frozen=True)
class PerfilEmpresa:
    """Lo único de la empresa que ve la IA. No tiene campo para la razón
    social ni para el NIF: no se pueden mandar aunque alguien quiera."""

    sector: str | None
    tamano: str | None
    ccaa: str | None
    descripcion: str | None
    palabras_clave: tuple[str, ...]
    tipo_persona: str

    @property
    def tiene_actividad(self) -> bool:
        return bool(self.descripcion or self.palabras_clave)

    @property
    def datos_que_faltan(self) -> tuple[str, ...]:
        """Los datos del perfil que importan para la idoneidad y están vacíos."""
        faltan = []
        if not self.ccaa:
            faltan.append(DATO_CCAA)
        if not self.sector:
            faltan.append(DATO_SECTOR)
        if not self.tamano:
            faltan.append(DATO_TAMANO)
        if not self.tiene_actividad:
            faltan.append(DATO_ACTIVIDAD)
        return tuple(faltan)

    @property
    def insuficiente(self) -> bool:
        """Faltan los cuatro datos: no tiene sentido pagar una llamada."""
        return len(self.datos_que_faltan) == 4


def perfil_desde_empresa(empresa: Empresa, palabras_clave: Iterable[str]) -> PerfilEmpresa:
    """El perfil que ve la IA. Del NIF solo se usa el tipo de persona, y la
    razón social no se lee."""
    return PerfilEmpresa(
        sector=_limpio(empresa.sector),
        tamano=_limpio(empresa.tamano),
        ccaa=_limpio(empresa.ccaa),
        descripcion=_limpio(empresa.descripcion),
        palabras_clave=tuple(palabra for palabra in (_limpio(p) for p in palabras_clave) if palabra),
        tipo_persona=tipo_persona(empresa.nif),
    )


async def cargar_perfil(db: AsyncSession, empresa_id: int) -> PerfilEmpresa | None:
    """El perfil de una empresa por su id, o None si no existe."""
    empresa = await db.get(Empresa, empresa_id)
    if empresa is None:
        return None
    palabras = (
        await db.execute(
            select(EmpresaPalabraClave.palabra)
            .where(EmpresaPalabraClave.empresa_id == empresa_id)
            .order_by(EmpresaPalabraClave.id)
        )
    ).scalars()
    return perfil_desde_empresa(empresa, palabras)


def ficha_a_texto(ficha: DetalleConvocatoriaBdns) -> str:
    """La ficha de la BDNS como texto para la IA.

    No lleva nada que dependa del día en que se consulta: el resumen y los
    requisitos se comparten entre empresas y se guardan, y un "plazo abierto"
    dejaría de ser cierto. La fecha de hoy solo va en la llamada de idoneidad.
    """
    # El separador no puede ser ">": la limpieza de etiquetas lo cambiaría.
    organo = " — ".join(nivel for nivel in (ficha.nivel1, ficha.nivel2, ficha.nivel3) if nivel)
    documentos = [
        f"{documento.descripcion or 'Documento'} ({documento.nombre_fichero})"
        if documento.nombre_fichero
        else documento.descripcion or ""
        for documento in ficha.documentos
    ]
    lineas = [
        "<convocatoria>",
        _linea("Código BDNS", ficha.codigo_bdns),
        _linea("Título", ficha.titulo),
        _linea("Órgano convocante", organo),
        _linea("Tipo de convocatoria", ficha.tipo_convocatoria),
        _linea("Finalidad", ficha.finalidad),
        _lista("Instrumentos", ficha.instrumentos),
        _lista("Tipos de beneficiario", ficha.tipos_beneficiario, maximo=MAX_ELEMENTOS_CRITERIO),
        _lista("Sectores", ficha.sectores, maximo=MAX_ELEMENTOS_CRITERIO),
        _lista("Regiones", ficha.regiones, maximo=MAX_ELEMENTOS_CRITERIO),
        _linea("Presupuesto total", _euros(ficha.presupuesto_total)),
        _linea(
            "Inicio del plazo de solicitud",
            _fecha_o_texto(ficha.fecha_inicio_solicitud, ficha.texto_inicio_solicitud),
        ),
        _linea("Fin del plazo de solicitud", _fecha_o_texto(ficha.fecha_fin_solicitud, ficha.texto_fin_solicitud)),
        _linea("Financiada con fondos del Plan de Recuperación (MRR)", "sí" if ficha.financiada_mrr else "no"),
        _linea("Reglamento europeo aplicable", ficha.reglamento),
        _linea("Ayuda de Estado", ficha.ayuda_estado),
        _lista("Fondos", ficha.fondos),
        _lista("Objetivos", ficha.objetivos),
        _linea("Bases reguladoras (referencia)", ficha.bases_reguladoras),
        _lista("Documentos publicados", [documento for documento in documentos if documento], maximo=MAX_DOCUMENTOS),
        "</convocatoria>",
    ]
    return "\n".join(lineas)


def perfil_a_texto(perfil: PerfilEmpresa) -> str:
    tamano = TAMANOS.get(perfil.tamano, perfil.tamano) if perfil.tamano else None
    lineas = [
        "<perfil_empresa>",
        _linea("Tipo de persona", perfil.tipo_persona),
        _linea("Comunidad autónoma", perfil.ccaa),
        _linea("Sector", perfil.sector),
        _linea("Tamaño", tamano),
        _linea("Descripción de la actividad", perfil.descripcion, limite=MAX_CARACTERES_DESCRIPCION_EMPRESA),
        _lista("Palabras clave", perfil.palabras_clave, maximo=MAX_PALABRAS_CLAVE),
        "</perfil_empresa>",
    ]
    return "\n".join(lineas)


def _linea(etiqueta: str, valor: str | None, *, limite: int = MAX_CARACTERES_CAMPO) -> str:
    texto = _limpio(valor)
    return f"{etiqueta}: {_recortar(texto, limite) if texto else NO_CONSTA}"


def _lista(etiqueta: str, valores: Iterable[str], *, maximo: int = MAX_ELEMENTOS_LISTA) -> str:
    elementos = [texto for texto in (_limpio(valor) for valor in valores) if texto]
    if not elementos:
        return f"{etiqueta}: {NO_CONSTA}"
    lineas = [f"- {_recortar(elemento, MAX_CARACTERES_ELEMENTO)}" for elemento in elementos[:maximo]]
    if len(elementos) > maximo:
        lineas.append(f"- (y {len(elementos) - maximo} más)")
    return "\n".join([f"{etiqueta}:", *lineas])


# `<`, `>` y los caracteres que se les parecen → `‹`, `›`.
_SIN_ETIQUETAS = str.maketrans({**dict.fromkeys("<＜﹤〈⟨⧼", "‹"), **dict.fromkeys(">＞﹥〉⟩⧽", "›")})


def _limpio(valor: str | None) -> str | None:
    """Sin espacios sobrantes y sin nada que pueda cerrar una etiqueta."""
    if valor is None:
        return None
    texto = " ".join(str(valor).split()).translate(_SIN_ETIQUETAS)
    return texto or None


def _recortar(texto: str, limite: int) -> str:
    if len(texto) <= limite:
        return texto
    return texto[: limite - 1].rstrip() + "…"


def _euros(importe: Decimal | None) -> str | None:
    if importe is None:
        return None
    # Formato español: punto de miles y coma decimal.
    cifra = f"{importe:,.0f}" if importe == importe.to_integral_value() else f"{importe:,.2f}"
    return cifra.replace(",", "_").replace(".", ",").replace("_", ".") + " €"


def _fecha_o_texto(fecha: date | None, texto: str | None) -> str | None:
    return fecha.isoformat() if fecha else texto
