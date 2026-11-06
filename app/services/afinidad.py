"""Affinity between a company and a grant (N3). **Pure**: no database, no network, no AI, no stored scores.

The score is deterministic and explainable (plan decision 7): 100 points in five blocks, each one with the reason
that earned it (`motivos`) or the doubt it leaves (`dudas`). Every sentence is a true statement about the data the
score was computed from; a missing profile field costs points and says so, it is never guessed.

| Block | Points | Rule |
|---|---|---|
| CNAE activity | 40 | the grant's CNAE sections include the company's; no sector declared = open to all (70 %) |
| Zone | 20 | same community 20, state-wide 14, another community 0 |
| Size / beneficiary | 15 | the beneficiary types BDNS declares against the company size |
| Interests | 15 | an interest the company marked points to the grant's `tema` |
| Deadline | 10 | margin to prepare the application |

Pieces of information that are unknown count as "no evidence" (0 points) except where the rule below says
otherwise (beneficiary types not declared: half the points, because most grants do not declare any).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA
from app.services.zonas import ZONA_ESTATAL, nombre_de_zona, normalizar

PUNTOS_CNAE = 40
PUNTOS_ZONA = 20
PUNTOS_TAMANO = 15
PUNTOS_INTERESES = 15
PUNTOS_PLAZO = 10

PUNTOS_CNAE_ABIERTA = 28  # no sector declared: open to every sector, 70 % of the block
PUNTOS_ZONA_ESTATAL = 14
PUNTOS_TAMANO_NEUTRO = 7  # the grant declares no beneficiary types
DIAS_CIERRE_CERCANO = 10
DIAS_MARGEN_AMPLIO = 21

# Which `tema` of the grant each interest of `GET /catalogos/intereses` points to (same table as the frontend).
TEMA_DE_INTERES: dict[str, str] = {
    "erp": "dig",
    "automatizacion": "dig",
    "ciberseguridad": "dig",
    "energia": "ene",
    "exportacion": "int",
    "formacion": "emp",
    "id_producto": "id",
    "contratacion": "emp",
}
NOMBRE_DE_INTERES: dict[str, str] = {
    "erp": "ERP y gestión",
    "automatizacion": "Automatización",
    "energia": "Energía",
    "ciberseguridad": "Ciberseguridad",
    "exportacion": "Exportación",
    "formacion": "Formación",
    "id_producto": "I+D de producto",
    "contratacion": "Contratación",
}
TAMANOS_PYME = ("autonomo", "micro", "pequena", "mediana")


@dataclass(frozen=True)
class PerfilAfinidad:
    """What the score reads from the company."""

    actividad_codigo: str | None = None  # CNAE section letter, `OTRA` or `None` (pending)
    tamano: str | None = None
    comunidad: str | None = None  # `zonas` community id; `None` when unknown
    intereses: frozenset[str] = frozenset()  # codes of `interes`


@dataclass(frozen=True)
class SenalesConvocatoria:
    """What the score reads from the grant."""

    tema: str = "otro"
    cnae_secciones: Sequence[str] = ()
    tiene_sectores: bool = False  # BDNS lists sectors (resolved to sections or not)
    comunidades: Sequence[str] = ()  # communities the grant covers, `zonas` ids
    estatal: bool = False
    beneficiarios: Sequence[str] = ()
    dias_restantes: int | None = None  # `None`: no deadline


@dataclass(frozen=True)
class ResultadoAfinidad:
    afinidad: int
    motivos: list[str] = field(default_factory=list)
    dudas: list[str] = field(default_factory=list)
    zona: str | None = None  # the zone to show: the company's community when covered, else the first one


def _actividad(perfil: PerfilAfinidad, senales: SenalesConvocatoria, motivos: list[str], dudas: list[str]) -> int:
    seccion = perfil.actividad_codigo
    if seccion is None or seccion == CODIGO_ACTIVIDAD_OTRA:
        dudas.append("Indica la actividad principal (CNAE) de tu empresa para comprobar el sector")
        return 0
    if seccion in senales.cnae_secciones:
        motivos.append("Encaja con la actividad de tu empresa")
        return PUNTOS_CNAE
    if not senales.cnae_secciones and not senales.tiene_sectores:
        motivos.append("Abierta a todos los sectores")
        return PUNTOS_CNAE_ABIERTA
    if not senales.cnae_secciones:
        dudas.append("Declara sectores que aún no hemos podido comprobar contra tu actividad")
        return 0
    dudas.append("Va dirigida a otros sectores de actividad")
    return 0


def _zona(
    perfil: PerfilAfinidad, senales: SenalesConvocatoria, motivos: list[str], dudas: list[str]
) -> tuple[int, str | None]:
    propia = perfil.comunidad
    if propia is not None and propia in senales.comunidades:
        motivos.append(f"Sede en {nombre_de_zona(propia)}")
        return PUNTOS_ZONA, propia
    if senales.comunidades:
        otra = senales.comunidades[0]
        if senales.estatal:
            motivos.append("Ámbito estatal")
            return PUNTOS_ZONA_ESTATAL, otra
        if propia is None:
            dudas.append("Indica tu provincia para comprobar si la ayuda es de tu zona")
        else:
            dudas.append(f"Es de otra comunidad ({nombre_de_zona(otra)})")
        return 0, otra
    if senales.estatal:
        motivos.append("Ámbito estatal")
        return PUNTOS_ZONA_ESTATAL, ZONA_ESTATAL
    dudas.append("No indica la zona a la que se dirige")
    return 0, None


def _tipos_beneficiario(beneficiarios: Sequence[str]) -> tuple[bool, bool, bool]:
    """(pyme/self-employed, large company, only-without-economic-activity markers) from the BDNS labels."""
    textos = [normalizar(b) for b in beneficiarios]
    pyme = any("pyme" in t for t in textos)
    grande = any("gran empresa" in t for t in textos)
    sin_actividad = any("no desarrollan actividad economica" in t for t in textos)
    return pyme, grande, sin_actividad and not (pyme or grande)


def _tamano(perfil: PerfilAfinidad, senales: SenalesConvocatoria, motivos: list[str], dudas: list[str]) -> int:
    if not senales.beneficiarios:
        return PUNTOS_TAMANO_NEUTRO
    pyme, grande, solo_sin_actividad = _tipos_beneficiario(senales.beneficiarios)
    if solo_sin_actividad:
        dudas.append("Va dirigida a entidades y personas sin actividad económica")
        return 0
    if not (pyme or grande):
        return PUNTOS_TAMANO_NEUTRO  # other types: nothing certain to say
    tamano = perfil.tamano
    if tamano is None:
        dudas.append("Indica el tamaño de tu empresa para comprobar a quién se dirige")
        return 0
    if tamano in TAMANOS_PYME and pyme:
        motivos.append("Dirigida a pymes y autónomos")
        return PUNTOS_TAMANO
    if tamano == "grande" and grande:
        motivos.append("Dirigida a gran empresa")
        return PUNTOS_TAMANO
    dudas.append("Está pensada para pymes y autónomos" if pyme else "Está pensada para gran empresa")
    return 0


def _intereses(perfil: PerfilAfinidad, senales: SenalesConvocatoria, motivos: list[str], dudas: list[str]) -> int:
    coinciden = sorted(c for c in perfil.intereses if TEMA_DE_INTERES.get(c) == senales.tema)
    if coinciden:
        motivos.append("Interés: " + ", ".join(NOMBRE_DE_INTERES[c].lower() for c in coinciden))
        return PUNTOS_INTERESES
    if not perfil.intereses:
        dudas.append("Aún no has marcado intereses: añádelos para afinar la afinidad")
    elif senales.tema != "otro":
        dudas.append("No coincide con los intereses de tu perfil")
    return 0


def _plazo(senales: SenalesConvocatoria, dudas: list[str]) -> int:
    dias = senales.dias_restantes
    if dias is None:
        return PUNTOS_PLAZO // 2  # no deadline: nothing forces a rush
    if dias < 0:
        dudas.append("El plazo de solicitud ya ha terminado")
        return 0
    if dias < DIAS_CIERRE_CERCANO:
        unidad = "día" if dias == 1 else "días"
        dudas.append(f"Cierra en {dias} {unidad}: poco margen para preparar la solicitud")
        return 2
    return PUNTOS_PLAZO if dias >= DIAS_MARGEN_AMPLIO else 6


def calcular(perfil: PerfilAfinidad, senales: SenalesConvocatoria) -> ResultadoAfinidad:
    """Score 0..100 with its reasons and doubts, in the order of the table above."""
    motivos: list[str] = []
    dudas: list[str] = []
    puntos = _actividad(perfil, senales, motivos, dudas)
    puntos_zona, zona = _zona(perfil, senales, motivos, dudas)
    puntos += puntos_zona
    puntos += _tamano(perfil, senales, motivos, dudas)
    puntos += _intereses(perfil, senales, motivos, dudas)
    puntos += _plazo(senales, dudas)
    return ResultadoAfinidad(afinidad=max(0, min(100, puntos)), motivos=motivos, dudas=dudas, zona=zona)
