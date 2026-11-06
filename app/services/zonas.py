"""Zones for the affinity score (N3). **Pure**: no database, no network.

The zone vocabulary is the one the frontend map draws: the 16 communities of `REGIONES_MAPA` (`and`, `ara`, ...) plus
`ES` for "all of Spain". Two sources are normalised into it:

- a grant's BDNS `region_ids`, through the catalogue nodes that are NUTS-2 communities (`ES11`..`ES70`, see
  `NUTS2_A_COMUNIDAD`); the DB glue builds the id -> community map from `catalogo_bdns` (`radar.mapa_comunidades`);
- the company's `ccaa` and `provincia`, which are **free text** (no catalogue): matched on accent-free keywords, the
  same ones the frontend uses for communities, plus the 50 provinces.

Ceuta and Melilla (`ES63`, `ES64`) are not on the map and have no zone: they resolve to `None` ("unknown").
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping

ZONA_ESTATAL = "ES"

NOMBRES_COMUNIDAD: Mapping[str, str] = {
    "and": "Andalucía",
    "ara": "Aragón",
    "ast": "Asturias",
    "bal": "Illes Balears",
    "can": "Canarias",
    "cnt": "Cantabria",
    "clm": "Castilla-La Mancha",
    "cyl": "Castilla y León",
    "cat": "Cataluña",
    "val": "C. Valenciana",
    "ext": "Extremadura",
    "gal": "Galicia",
    "mad": "Madrid",
    "mur": "Murcia",
    "nav": "Navarra",
    "pva": "País Vasco",
    "rio": "La Rioja",
}

# NUTS-2 code (the 4-character `catalogo_bdns.codigo` of a region node) -> community.
NUTS2_A_COMUNIDAD: Mapping[str, str] = {
    "ES11": "gal", "ES12": "ast", "ES13": "cnt", "ES21": "pva", "ES22": "nav", "ES23": "rio",
    "ES24": "ara", "ES30": "mad", "ES41": "cyl", "ES42": "clm", "ES43": "ext", "ES51": "cat",
    "ES52": "val", "ES53": "bal", "ES61": "and", "ES62": "mur", "ES70": "can",
}  # fmt: skip

# Same fragments as the frontend (`REGIONES_MAPA.claves`), lower-case and accent-free.
_CLAVES_COMUNIDAD: Mapping[str, tuple[str, ...]] = {
    "and": ("andalu",),
    "ara": ("aragon",),
    "ast": ("asturias",),
    "bal": ("balear", "illes"),
    "can": ("canaria",),
    "cnt": ("cantabria",),
    "clm": ("la mancha", "castilla-la"),
    "cyl": ("castilla y leon", "castilla-leon"),
    "cat": ("catalu", "catalunya"),
    "val": ("valencia", "alicante", "castellon"),
    "ext": ("extremadura",),
    "gal": ("galicia", "xunta"),
    "mad": ("madrid",),
    "mur": ("murcia",),
    "nav": ("navarra", "nafarroa"),
    "pva": ("pais vasco", "euskadi", "vizcaya", "bizkaia", "gipuzkoa", "guipuzcoa", "alava", "araba"),
    "rio": ("rioja",),
}

# Provinces whose name does not contain their community's key (the ones that do are caught by the keys above).
_PROVINCIAS: Mapping[str, str] = {
    "almeria": "and", "cadiz": "and", "cordoba": "and", "granada": "and", "huelva": "and", "jaen": "and",
    "malaga": "and", "sevilla": "and",
    "huesca": "ara", "teruel": "ara", "zaragoza": "ara",
    "las palmas": "can", "santa cruz de tenerife": "can", "tenerife": "can",
    "albacete": "clm", "ciudad real": "clm", "cuenca": "clm", "guadalajara": "clm", "toledo": "clm",
    "avila": "cyl", "burgos": "cyl", "leon": "cyl", "palencia": "cyl", "salamanca": "cyl", "segovia": "cyl",
    "soria": "cyl", "valladolid": "cyl", "zamora": "cyl",
    "barcelona": "cat", "girona": "cat", "gerona": "cat", "lleida": "cat", "lerida": "cat", "tarragona": "cat",
    "badajoz": "ext", "caceres": "ext",
    "a coruna": "gal", "la coruna": "gal", "coruna": "gal", "lugo": "gal", "ourense": "gal", "orense": "gal",
    "pontevedra": "gal",
    "mallorca": "bal", "menorca": "bal", "ibiza": "bal", "formentera": "bal",
}  # fmt: skip


def normalizar(texto: str) -> str:
    """Lower-case, accent-free, single spaces."""
    sin_acentos = "".join(c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sin_acentos.lower()).strip()


def _contiene(texto: str, clave: str) -> bool:
    # The key must start a word ("illes" must not match "villes"); it may continue ("andalu" in "andalucia").
    return re.search(rf"(?<![a-z]){re.escape(clave)}", texto) is not None


def comunidad_de_texto(texto: str | None) -> str | None:
    """The community named (or implied by a province) in free text, or `None`."""
    if texto is None or not texto.strip():
        return None
    t = normalizar(texto)
    for comunidad, claves in _CLAVES_COMUNIDAD.items():
        if any(_contiene(t, clave) for clave in claves):
            return comunidad
    for provincia, comunidad in _PROVINCIAS.items():
        if _contiene(t, provincia):
            return comunidad
    return None


def comunidad_de_empresa(ccaa: str | None, provincia: str | None) -> str | None:
    """The company's community. `ccaa` wins; the province fills in when it is empty or unrecognised."""
    return comunidad_de_texto(ccaa) or comunidad_de_texto(provincia)


def comunidad_de_nuts(codigo: str | None) -> str | None:
    """Community of a catalogue region node by its NUTS code: only the 4-character community node itself counts."""
    if codigo is None:
        return None
    return NUTS2_A_COMUNIDAD.get(codigo.strip().upper())


def comunidades_de_regiones(region_ids: Iterable[int], por_region: Mapping[int, str]) -> tuple[str, ...]:
    """Communities a grant covers, in the order of `region_ids`, without repeats."""
    vistas: dict[str, None] = {}
    for region_id in region_ids:
        comunidad = por_region.get(region_id)
        if comunidad is not None:
            vistas.setdefault(comunidad)
    return tuple(vistas)


def nombre_de_zona(zona: str | None) -> str:
    if zona == ZONA_ESTATAL:
        return "toda España"
    return NOMBRES_COMUNIDAD.get(zona or "", "otra comunidad")
