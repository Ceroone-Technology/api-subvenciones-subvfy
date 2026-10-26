"""Catálogos de órganos y regiones de la BDNS (`bdns-local-sync`).

Dos trabajos:

- **Refrescar** `catalogo_bdns` desde `/organos?idAdmon=C|A|L|O` y `/regiones`
  (5 llamadas). Se hace un upsert por `(tipo, bdns_id)` y **nunca se borra**: las
  alertas guardan esos ids (D12). Las 5 llamadas se hacen antes de escribir, así
  que un fallo de la BDNS no deja el catálogo a medias.
- **Resolver** los textos del detalle a ids (D2, D3). El detalle no trae el id
  del órgano, solo `nivel1/2/3` en texto, y las regiones llevan el código NUTS
  como prefijo (`"ES12 - PRINCIPADO DE ASTURIAS"`). `organo_ids`/`region_ids` de
  `convocatoria` guardan el nodo y todos sus ancestros, de modo que filtrar por
  un padre encuentra a sus descendientes.

La resolución es **exacta sobre texto normalizado** (mayúsculas, sin acentos,
espacios colapsados), sin coincidencias aproximadas. Lo que no se resuelve se
cuenta (`organos_sin_resolver`) para que el desajuste sea visible en `sync_run`.
"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CatalogoBdns
from app.services.bdns_consulta import NIVEL_A_TIPO_ADMINISTRACION
from app.services.bdns_mapper import RutaOrgano
from app.services.bdns_sync_cliente import ClienteBdnsSync, NivelAdmon
from app.services.deduplicacion_alertas import NIVEL_BDNS_A_NUESTRO

SEPARADOR_RUTA = " / "
# `"ES12 - ..."`, `"ES - ..."`, `"ESZ - ..."`: el código NUTS antes del guion.
_PATRON_CODIGO = re.compile(r"^\s*([A-Z0-9]+)\s+-\s+")
# Un INSERT con 10 columnas por fila queda muy por debajo del límite de 32 767 parámetros.
FILAS_POR_LOTE = 1000
DESCRIPCION_MAX = 500


@dataclass(frozen=True)
class NodoCatalogo:
    tipo: str
    bdns_id: int
    padre_bdns_id: int | None
    descripcion: str
    codigo: str | None
    nivel_administracion: str | None
    # Raíz primero, el propio nodo al final: nodo + ancestros.
    ruta_ids: tuple[int, ...]
    ruta_norm: str


@dataclass(frozen=True)
class ResultadoRefresco:
    organos: int
    regiones: int


def normalizar_texto(texto: str) -> str:
    sin_acentos = "".join(c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c))
    return " ".join(sin_acentos.upper().split())


def aplanar_arbol(
    nodos: Sequence[dict[str, Any]], *, tipo: str, nivel_administracion: str | None = None
) -> list[NodoCatalogo]:
    """Árbol `{id, descripcion, children}` de la BDNS -> filas planas con su ruta.

    Un nodo sin `id` entero o sin descripción se salta (con sus hijos: sin padre
    válido no hay ruta fiable).
    """
    aplanados: list[NodoCatalogo] = []

    def recorrer(nodo: dict[str, Any], ruta_ids: tuple[int, ...], ruta_texto: tuple[str, ...]) -> None:
        identificador = nodo.get("id")
        descripcion = str(nodo.get("descripcion") or "").strip()
        if isinstance(identificador, bool) or not isinstance(identificador, int) or not descripcion:
            return
        ruta_ids = (*ruta_ids, identificador)
        ruta_texto = (*ruta_texto, normalizar_texto(descripcion))
        coincidencia = _PATRON_CODIGO.match(descripcion.upper()) if tipo == "region" else None
        aplanados.append(
            NodoCatalogo(
                tipo=tipo,
                bdns_id=identificador,
                padre_bdns_id=ruta_ids[-2] if len(ruta_ids) > 1 else None,
                descripcion=descripcion[:DESCRIPCION_MAX],
                codigo=coincidencia.group(1) if coincidencia else None,
                nivel_administracion=nivel_administracion,
                ruta_ids=ruta_ids,
                ruta_norm=SEPARADOR_RUTA.join(ruta_texto),
            )
        )
        for hijo in nodo.get("children") or []:
            if isinstance(hijo, dict):
                recorrer(hijo, ruta_ids, ruta_texto)

    for raiz in nodos:
        if isinstance(raiz, dict):
            recorrer(raiz, (), ())
    return aplanados


class ResolvedorCatalogo:
    """Resuelve textos del detalle a ids, en memoria. Los contadores acumulan por ejecución."""

    def __init__(self, nodos: Iterable[NodoCatalogo]) -> None:
        self.organos_sin_resolver = 0
        self.regiones_sin_resolver = 0
        self._regiones_por_codigo: dict[str, NodoCatalogo] = {}
        # (nivel, sufijo de la ruta de texto) -> nodos. El sufijo completo es la ruta
        # entera desde la raíz; los más cortos permiten que una raíz envolvente
        # ("ADMINISTRACION AUTONOMICA") no impida casar `nivel2 / nivel3`.
        self._organos_por_sufijo: dict[tuple[str, str], list[NodoCatalogo]] = defaultdict(list)
        for nodo in nodos:
            if nodo.tipo == "region":
                if nodo.codigo is not None:
                    self._regiones_por_codigo.setdefault(nodo.codigo, nodo)
            elif nodo.nivel_administracion is not None:
                segmentos = nodo.ruta_norm.split(SEPARADOR_RUTA)
                for i in range(len(segmentos)):
                    self._organos_por_sufijo[(nodo.nivel_administracion, SEPARADOR_RUTA.join(segmentos[i:]))].append(
                        nodo
                    )

    def resolver_regiones(self, etiquetas: Iterable[str]) -> list[int]:
        """Unión, sin repetir y en orden de aparición, de las rutas de cada región."""
        ids: list[int] = []
        for etiqueta in etiquetas:
            coincidencia = _PATRON_CODIGO.match(etiqueta.upper())
            nodo = self._regiones_por_codigo.get(coincidencia.group(1)) if coincidencia else None
            if nodo is None:
                self.regiones_sin_resolver += 1
                continue
            ids.extend(i for i in nodo.ruta_ids if i not in ids)
        return ids

    def resolver_organo(self, ruta: RutaOrgano) -> list[int]:
        """Ruta de ids del órgano; la parcial (solo `nivel2`) si `nivel3` no se encuentra."""
        nivel = NIVEL_BDNS_A_NUESTRO.get((ruta.nivel1 or "").upper())
        if nivel is None or not ruta.nivel2:
            self.organos_sin_resolver += 1
            return []
        nivel2 = normalizar_texto(ruta.nivel2)
        if not ruta.nivel3:
            return self._buscar(nivel, nivel2, contar=True)
        completo = self._buscar(nivel, f"{nivel2}{SEPARADOR_RUTA}{normalizar_texto(ruta.nivel3)}", contar=False)
        if completo:
            return completo
        # Parcial: el órgano de nivel 2 sí, el de nivel 3 no. Cuenta como sin resolver.
        self.organos_sin_resolver += 1
        return self._buscar(nivel, nivel2, contar=False)

    def _buscar(self, nivel: str, texto: str, *, contar: bool) -> list[int]:
        candidatos = self._organos_por_sufijo.get((nivel, texto), [])
        if len(candidatos) > 1:
            exactos = [n for n in candidatos if n.ruta_norm == texto]
            candidatos = exactos if len(exactos) == 1 else []
        if len(candidatos) == 1:
            return list(candidatos[0].ruta_ids)
        if contar:
            self.organos_sin_resolver += 1
        return []


async def refrescar_catalogos(db: AsyncSession, cliente: ClienteBdnsSync, *, usuario_id: int) -> ResultadoRefresco:
    """Descarga los 5 árboles y los upserta; hace commit al final."""
    organos: list[NodoCatalogo] = []
    for nivel, tipo_admon in NIVEL_A_TIPO_ADMINISTRACION.items():
        arbol = await cliente.organos(cast(NivelAdmon, tipo_admon))
        organos.extend(aplanar_arbol(arbol, tipo="organo", nivel_administracion=nivel))
    regiones = aplanar_arbol(await cliente.regiones(), tipo="region")

    await _upsert(db, organos, usuario_id)
    await _upsert(db, regiones, usuario_id)
    await db.commit()
    return ResultadoRefresco(organos=len(organos), regiones=len(regiones))


async def cargar_resolvedor(db: AsyncSession) -> ResolvedorCatalogo:
    filas = (await db.execute(select(CatalogoBdns))).scalars().all()
    return ResolvedorCatalogo(
        NodoCatalogo(
            tipo=f.tipo,
            bdns_id=f.bdns_id,
            padre_bdns_id=f.padre_bdns_id,
            descripcion=f.descripcion,
            codigo=f.codigo,
            nivel_administracion=f.nivel_administracion,
            ruta_ids=tuple(f.ruta_ids),
            ruta_norm=f.ruta_norm or "",
        )
        for f in filas
    )


async def _upsert(db: AsyncSession, nodos: list[NodoCatalogo], usuario_id: int) -> None:
    # Un mismo id repetido dentro de un INSERT haría fallar el ON CONFLICT: gana el último.
    unicos = {(n.tipo, n.bdns_id): n for n in nodos}
    filas = [
        {
            "tipo": n.tipo,
            "bdns_id": n.bdns_id,
            "padre_bdns_id": n.padre_bdns_id,
            "descripcion": n.descripcion,
            "codigo": n.codigo,
            "nivel_administracion": n.nivel_administracion,
            "ruta_ids": list(n.ruta_ids),
            "ruta_norm": n.ruta_norm,
            "created_by": usuario_id,
            "updated_by": usuario_id,
        }
        for n in unicos.values()
    ]
    for inicio in range(0, len(filas), FILAS_POR_LOTE):
        insercion = insert_postgresql(CatalogoBdns).values(filas[inicio : inicio + FILAS_POR_LOTE])
        actualizables = (
            "padre_bdns_id",
            "descripcion",
            "codigo",
            "nivel_administracion",
            "ruta_ids",
            "ruta_norm",
        )
        await db.execute(
            insercion.on_conflict_do_update(
                index_elements=[CatalogoBdns.tipo, CatalogoBdns.bdns_id],
                set_={
                    **{c: insercion.excluded[c] for c in actualizables},
                    "sincronizado_at": func.clock_timestamp(),
                    "updated_at": func.now(),
                    "updated_by": usuario_id,
                },
            )
        )
