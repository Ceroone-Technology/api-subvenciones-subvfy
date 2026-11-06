"""Lectura de los catálogos locales (`bdns-local-sync`, PR5). Solo lee la BD: nunca llama a la BDNS."""

from collections import defaultdict

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CatalogoBdns, Convocatoria
from app.schemas.catalogo import NodoCatalogo


async def arbol(db: AsyncSession, tipo: str, nivel: str | None = None) -> list[NodoCatalogo]:
    """Árbol `tipo` ordenado por descripción. Con `nivel`, solo los órganos de esa administración.

    Un nodo cuyo padre no está en el resultado cuenta como raíz, así que filtrar por nivel no pierde ramas.
    """
    consulta = select(CatalogoBdns.bdns_id, CatalogoBdns.padre_bdns_id, CatalogoBdns.descripcion).where(
        CatalogoBdns.tipo == tipo
    )
    if nivel:
        consulta = consulta.where(CatalogoBdns.nivel_administracion == nivel)
    filas = (await db.execute(consulta.order_by(CatalogoBdns.descripcion, CatalogoBdns.bdns_id))).all()
    presentes = {f.bdns_id for f in filas}
    hijos: dict[int | None, list[int]] = defaultdict(list)
    for f in filas:
        hijos[f.padre_bdns_id if f.padre_bdns_id in presentes and f.padre_bdns_id != f.bdns_id else None].append(
            f.bdns_id
        )
    nodos = {f.bdns_id: NodoCatalogo(id=f.bdns_id, descripcion=f.descripcion) for f in filas}
    for padre, ids in hijos.items():
        if padre is not None:
            nodos[padre].children = [nodos[i] for i in ids]
    return [nodos[i] for i in hijos[None]]


async def beneficiarios(db: AsyncSession) -> list[NodoCatalogo]:
    """Tipos de beneficiario distintos de las convocatorias vigentes, tal cual se filtran en la búsqueda."""
    desplegado = func.unnest(Convocatoria.beneficiarios).table_valued("valor").render_derived()
    valor = desplegado.c.valor
    consulta = select(valor).select_from(Convocatoria).join(desplegado, true())
    textos = await db.scalars(consulta.where(Convocatoria.removed_at.is_(None)).distinct().order_by(valor))
    return [NodoCatalogo(id=t, descripcion=t) for t in textos if t]
