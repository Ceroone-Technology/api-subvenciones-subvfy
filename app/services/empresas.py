"""Lectura y escritura del perfil ampliado de empresa (N2).

Los intereses viven en `empresa_interes`; se cargan de una página entera con **una** consulta `IN (ids)` (el modelo
`Empresa` no tiene `relationship()`, ver el patrón de alertas) y se ordenan según el catálogo.
"""

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Empresa, EmpresaInteres, Interes
from app.schemas.empresa import EmpresaRead, ItemPerfilRead
from app.services.perfil_empresa import completitud


async def intereses_de(db: AsyncSession, empresa_ids: Sequence[int]) -> dict[int, list[str]]:
    """Códigos de interés de cada empresa, en el orden del catálogo."""
    if not empresa_ids:
        return {}
    filas = await db.execute(
        select(EmpresaInteres.empresa_id, EmpresaInteres.interes_codigo)
        .join(Interes, Interes.codigo == EmpresaInteres.interes_codigo)
        .where(EmpresaInteres.empresa_id.in_(list(empresa_ids)))
        .order_by(EmpresaInteres.empresa_id, Interes.orden)
    )
    resultado: dict[int, list[str]] = {}
    for empresa_id, codigo in filas:
        resultado.setdefault(empresa_id, []).append(codigo)
    return resultado


async def leer(db: AsyncSession, empresas: Sequence[Empresa]) -> list[EmpresaRead]:
    por_empresa = await intereses_de(db, [e.id for e in empresas])
    salida = []
    for e in empresas:
        intereses = por_empresa.get(e.id, [])
        pct, pendientes = completitud(e, intereses)
        derivados = {
            "intereses": intereses,
            "perfil_completo_pct": pct,
            "perfil_pendiente": [ItemPerfilRead(codigo=i.codigo, etiqueta=i.etiqueta) for i in pendientes],
        }
        propios = {c: getattr(e, c) for c in EmpresaRead.model_fields if c not in derivados}
        salida.append(EmpresaRead.model_validate({**propios, **derivados}))
    return salida


async def reemplazar_intereses(db: AsyncSession, empresa_id: int, codigos: Sequence[str], usuario_id: int) -> None:
    """Deja exactamente `codigos` (sin repetidos, ya validados por el schema).

    `ON CONFLICT DO NOTHING`: dos PATCH simultáneos pueden borrar a la vez y volver a insertar la misma pareja;
    el segundo INSERT no debe reventar con un 500 (queda un resultado válido, el del último que escribe).
    """
    await db.execute(delete(EmpresaInteres).where(EmpresaInteres.empresa_id == empresa_id))
    if not codigos:
        return
    await db.execute(
        insert(EmpresaInteres)
        .values(
            [
                {"empresa_id": empresa_id, "interes_codigo": c, "created_by": usuario_id, "updated_by": usuario_id}
                for c in codigos
            ]
        )
        .on_conflict_do_nothing(index_elements=["empresa_id", "interes_codigo"])
    )
