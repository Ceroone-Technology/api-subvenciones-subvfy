"""Own classification of grants (N1): `tema` by rules and `gestiona_subvfy`.

**Pure part** (`clasificar_tema`, `gestiona_por_regla`): keyword rules over the title, the BDNS purpose
(`finalidad`), the objectives and, for two narrow cases, the sector descriptions. No database, no network.
Rules are checked in priority order (digital > I+D > energy > internationalisation > employment), so a grant that
mixes themes lands on the one Subvfy cares most about. When no keyword matches, the BDNS purpose decides.

**Database part** (`recalcular`, `aplicar_excepcion`): the rules are not part of `contenido_hash`, so when they
change (or the column is first added) the rows are recomputed with the CLI `recalcular-clasificacion`, which never
calls the BDNS. `gestiona_subvfy` = rule (`tema` in `TEMAS_GESTIONADOS`) unless the admin table
`gestion_excepcion` says otherwise.
"""

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import ScalarSelect, Select, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Convocatoria, GestionExcepcion
from app.models.convocatoria import TEMAS, TEMAS_GESTIONADOS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemaCatalogo:
    codigo: str
    nombre: str
    gestiona_subvfy: bool


_NOMBRES = {
    "dig": "Digitalización",
    "id": "I+D+i",
    "ene": "Energía y sostenibilidad",
    "int": "Internacionalización",
    "emp": "Empleo",
    "otro": "Otros",
}
CATALOGO_TEMAS: tuple[TemaCatalogo, ...] = tuple(
    TemaCatalogo(codigo, _NOMBRES[codigo], codigo in TEMAS_GESTIONADOS) for codigo in TEMAS
)


def _normalizar(texto: str) -> str:
    sin_acentos = "".join(c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c))
    return sin_acentos.lower()


_REGLAS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "dig",
        re.compile(
            r"digital|digitaliz|ciberseguridad|inteligencia artificial|\bia\b|software|informatic|telecomunicacion"
            r"|banda ancha|\b5g\b|industria 4\.0|comercio electronico|e-?commerce|\btics?\b|big data|blockchain"
            r"|internet de las cosas|\biot\b|robotica|cloud"
        ),
    ),
    (
        "id",
        re.compile(
            # Bare "investigacion", "innovacion" or "tecnologico" also describe health, police or university grants
            # that are not business R&D: they need a business-R&D context.
            r"\bi ?\+ ?d\b|\bid ?\+ ?i\b|desarrollo experimental|desarrollo tecnologico"
            r"|investigacion (industrial|aplicada|y desarrollo|, desarrollo|e innovacion)"
            r"|proyectos? de (investigacion|innovacion)"
            r"|innovacion (empresarial|tecnologica|abierta|en (las |los )?(pymes?|empresas?|procesos|productos))"
            r"|(ayudas?|subvenciones?|apoyo) (a|para) la innovacion|empresas? innovadoras?"
            r"|transferencia (de conocimiento|tecnologica)|centros? tecnologicos?|parques? (cientificos|tecnologicos)"
        ),
    ),
    (
        "ene",
        re.compile(
            r"energi|energetic|renovable|fotovoltaic|autoconsumo|descarboniz|hidrogeno|biomasa|eolic"
            r"|vehiculos? electrico|movilidad electrica|puntos? de recarga|economia circular|cambio climatico"
            r"|medio ?ambient|residuos|reciclaje|huella de carbono|transicion (ecologica|verde)"
        ),
    ),
    (
        "int",
        re.compile(
            r"internacionalizacion|exportacion|exportador|exportar|comercio exterior"
            r"|mercados? (exteriores|internacionales)"
            r"|promocion exterior|proyeccion exterior"
        ),
    ),
    (
        "emp",
        re.compile(
            # Bare "empleo" also names public-sector jobs; see `_NO_EMP`.
            r"fomento del empleo|(creacion|generacion|mantenimiento) de empleo"
            r"|empleo (estable|de calidad|juvenil|local)"
            r"|(plan|programa)s? de empleo|desempleo|desempleados|insercion laboral|autoempleo|politicas activas"
            r"|contratacion (indefinida|de (personas|trabajadores|jovenes|titulados|desempleados|mujeres|parados))"
        ),
    ),
)
# "Internationalisation" of a university for its students is not a company-export grant.
_NO_INT = re.compile(r"estudiant|alumn|movilidad")
# Public-sector recruitment is not an employment-promotion grant.
_NO_EMP = re.compile(r"empleo publico|oferta de empleo|bolsa de empleo|proceso selectivo|funcionari")
_SECTOR_DIG = re.compile(r"informatic|programacion, consultoria")
_SECTOR_ID = re.compile(r"investigacion y desarrollo")
_POR_FINALIDAD = {
    "industria y energia": "ene",
    "fomento del empleo": "emp",
    "desempleo": "emp",
}


def clasificar_tema(
    *,
    titulo: str | None,
    finalidad: str | None = None,
    objetivos: Sequence[str] = (),
    sectores: Sequence[str] = (),
) -> str:
    texto = _normalizar(" ".join(p for p in (titulo, finalidad, *objetivos) if p))
    for tema, patron in _REGLAS:
        if not patron.search(texto):
            continue
        if (tema == "int" and _NO_INT.search(texto)) or (tema == "emp" and _NO_EMP.search(texto)):
            continue
        return tema
    texto_sectores = _normalizar(" ".join(sectores))
    if _SECTOR_DIG.search(texto_sectores):
        return "dig"
    if _SECTOR_ID.search(texto_sectores):
        return "id"
    return _POR_FINALIDAD.get(_normalizar(finalidad or "").strip(), "otro")


# A digital or R&D title is not enough: Subvfy manages grants that companies can apply to for a business project.
_NO_ES_EMPRESA = re.compile(r"no desarrollan actividad economica")
_FINALIDADES_SOCIALES = frozenset(
    {"servicios sociales y promocion social", "sanidad", "educacion", "cultura", "defensa"}
)


def gestiona_por_regla(tema: str, *, beneficiarios: Sequence[str] = (), finalidad: str | None = None) -> bool:
    """Rule for `gestiona_subvfy`: a managed theme, unless beneficiaries are all non-economic or the purpose is social.

    Unknown data never blocks: no beneficiaries listed (a list-only row) or no purpose keeps the theme rule.
    Example: BDNS 936275 "Programa Aldeas Digitales" (beneficiaries "personas juridicas que no desarrollan actividad
    economica", purpose "Servicios Sociales y Promocion Social") has theme `dig` but is not managed.
    """
    if tema not in TEMAS_GESTIONADOS:
        return False
    textos = [_normalizar(b) for b in beneficiarios]
    if textos and all(_NO_ES_EMPRESA.search(t) for t in textos):
        return False
    return _normalizar(finalidad or "").strip() not in _FINALIDADES_SOCIALES


def excepcion_de() -> ScalarSelect[bool]:
    """Scalar subquery with the admin override for a grant row (NULL when there is none)."""
    return (
        select(GestionExcepcion.gestiona)
        # Raw column on purpose: SQLAlchemy cannot auto-correlate inside INSERT .. ON CONFLICT, and a mapped
        # `Convocatoria.id` would add `convocatoria` to this subquery FROM (every exception would match every row).
        .where(GestionExcepcion.convocatoria_id == literal_column("convocatoria.id"))
        .scalar_subquery()
    )


# --- Database part ---------------------------------------------------------------------------------


@dataclass
class ResultadoRecalculo:
    total: int = 0
    cambiadas: int = 0
    por_tema: Counter[str] = field(default_factory=Counter)
    gestionadas: int = 0


async def recalcular(db: AsyncSession, *, prefijo: str = "", lote: int = 1000) -> ResultadoRecalculo:
    """Recomputes `tema` and `gestiona_subvfy` of every grant (retired ones included). Offline and idempotent.

    `prefijo` restricts it to codes that start with it (tests use it to leave real rows alone).
    """
    c = Convocatoria
    resultado = ResultadoRecalculo()
    ultimo_id = 0
    while True:
        consulta: Select = (
            select(
                c.id,
                c.titulo,
                c.finalidad,
                c.detalle["objetivos"].label("objetivos"),
                c.sectores,
                c.beneficiarios,
                c.tema,
                c.gestiona_subvfy,
                excepcion_de().label("excepcion"),
            )
            .where(c.id > ultimo_id, c.codigo_bdns.like(f"{prefijo}%"))
            .order_by(c.id)
            .limit(lote)
        )
        filas = (await db.execute(consulta)).all()
        if not filas:
            break
        ultimo_id = filas[-1].id
        cambios: dict[tuple[str, bool], list[int]] = {}
        for f in filas:
            objetivos = [o for o in (f.objetivos or []) if isinstance(o, str)]
            tema = clasificar_tema(titulo=f.titulo, finalidad=f.finalidad, objetivos=objetivos, sectores=f.sectores)
            regla = gestiona_por_regla(tema, beneficiarios=f.beneficiarios, finalidad=f.finalidad)
            gestiona = f.excepcion if f.excepcion is not None else regla
            resultado.total += 1
            resultado.por_tema[tema] += 1
            resultado.gestionadas += gestiona
            if (tema, gestiona) != (f.tema, f.gestiona_subvfy):
                cambios.setdefault((tema, gestiona), []).append(f.id)
        for (tema, gestiona), ids in cambios.items():
            await db.execute(
                update(c)
                .where(c.id.in_(ids))
                .values(tema=tema, gestiona_subvfy=gestiona)
                .execution_options(synchronize_session=False)
            )
            resultado.cambiadas += len(ids)
        await db.commit()
    logger.info("Clasificación: %d filas, %d cambiadas.", resultado.total, resultado.cambiadas)
    return resultado


async def fijar_excepcion(
    db: AsyncSession, *, convocatoria_id: int, gestiona: bool | None, motivo: str | None, usuario_id: int
) -> None:
    """Creates, updates (`gestiona` bool) or removes (`None`) the override and refreshes the stored flag."""
    existente = await db.scalar(
        select(GestionExcepcion).where(GestionExcepcion.convocatoria_id == convocatoria_id)
    )
    if gestiona is None:
        if existente is not None:
            await db.delete(existente)
    elif existente is None:
        db.add(
            GestionExcepcion(
                convocatoria_id=convocatoria_id, gestiona=gestiona, motivo=motivo,
                created_by=usuario_id, updated_by=usuario_id,
            )  # fmt: skip
        )
    else:
        existente.gestiona, existente.motivo, existente.updated_by = gestiona, motivo, usuario_id
    await db.flush()
    c = Convocatoria
    efectivo = gestiona
    if efectivo is None:
        tema, beneficiarios, finalidad = (
            await db.execute(select(c.tema, c.beneficiarios, c.finalidad).where(c.id == convocatoria_id))
        ).one()
        efectivo = gestiona_por_regla(tema, beneficiarios=beneficiarios, finalidad=finalidad)
    await db.execute(
        update(c)
        .where(c.id == convocatoria_id)
        .values(gestiona_subvfy=efectivo, updated_by=usuario_id, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )
