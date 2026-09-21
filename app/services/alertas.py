"""Mutaciones de alerta (Hito 4, Funcionalidad 1).

**Las alertas son personales**, como los favoritos: cuelgan de
`alerta.usuario_id` y solo su propietario las edita o borra. Cualquier otro,
admin incluido, recibe lo mismo que con un id inexistente
(`AlertaNoEncontrada` → 404): no se confirma que exista una alerta ajena.

**Los filtros viven en tablas hijas** (`alerta_organo`, `alerta_region`), una
fila por id de la BDNS. En la API se ven como listas dentro de la alerta; al
editarlas, la lista nueva reemplaza entera a la anterior.

El servicio no conoce HTTP: señala los errores con excepciones de dominio y
el router las traduce.
"""

from datetime import date

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alerta, AlertaOrgano, AlertaRegion
from app.schemas.alerta import AlertaCreate, AlertaRead, AlertaUpdate


class AlertaNoEncontrada(Exception):
    """No existe, o no es de este usuario: a propósito, son indistinguibles."""


class RangoFechasInvalido(ValueError):
    """`fecha_desde` > `fecha_hasta` una vez fusionado el PATCH con lo guardado."""


async def crear_alerta(db: AsyncSession, datos: AlertaCreate, usuario_id: int) -> AlertaRead:
    alerta = Alerta(
        **datos.model_dump(exclude={"organos", "regiones"}),
        usuario_id=usuario_id,
        created_by=usuario_id,
        updated_by=usuario_id,
    )
    db.add(alerta)
    await db.flush()  # para tener alerta.id antes de insertar los filtros

    _anadir_filtros(db, alerta.id, datos.organos, datos.regiones, usuario_id)
    await db.commit()
    return await _leer(db, alerta.id)


async def actualizar_alerta(
    db: AsyncSession, alerta_id: int, datos: AlertaUpdate, usuario_id: int
) -> AlertaRead:
    alerta = (
        await db.execute(select(Alerta).where(Alerta.id == alerta_id, Alerta.usuario_id == usuario_id))
    ).scalar_one_or_none()
    if alerta is None:
        raise AlertaNoEncontrada

    cambios = datos.model_dump(exclude_unset=True)
    organos: list[int] | None = cambios.pop("organos", None)
    regiones: list[int] | None = cambios.pop("regiones", None)

    # El schema solo ve el body: con un PATCH que trae únicamente fecha_desde,
    # la comparación tiene que hacerse contra la fecha_hasta ya guardada.
    desde: date | None = cambios.get("fecha_desde", alerta.fecha_desde)
    hasta: date | None = cambios.get("fecha_hasta", alerta.fecha_hasta)
    if desde is not None and hasta is not None and desde > hasta:
        raise RangoFechasInvalido(f"fecha_desde ({desde}) no puede ser posterior a fecha_hasta ({hasta}).")

    for campo, valor in cambios.items():
        setattr(alerta, campo, valor)

    if organos is not None:
        await db.execute(delete(AlertaOrgano).where(AlertaOrgano.alerta_id == alerta_id))
    if regiones is not None:
        await db.execute(delete(AlertaRegion).where(AlertaRegion.alerta_id == alerta_id))
    _anadir_filtros(db, alerta_id, organos or [], regiones or [], usuario_id)

    # Firma explícita: si solo cambian los filtros (filas hijas), la fila de
    # la alerta no queda sucia y el `onupdate` de updated_at no saltaría.
    await db.execute(
        update(Alerta)
        .where(Alerta.id == alerta_id)
        .values(updated_at=func.now(), updated_by=usuario_id)
    )
    await db.commit()
    return await _leer(db, alerta_id)


async def eliminar_alerta(db: AsyncSession, alerta_id: int, usuario_id: int) -> None:
    """Borrado físico. Filtros y ejecuciones caen por ON DELETE CASCADE; para
    dejar de recibir avisos sin perder nada está `activa = false`."""
    borrada = (
        await db.execute(
            delete(Alerta)
            .where(Alerta.id == alerta_id, Alerta.usuario_id == usuario_id)
            .returning(Alerta.id)
        )
    ).first()
    if borrada is None:
        raise AlertaNoEncontrada
    await db.commit()


def _anadir_filtros(
    db: AsyncSession, alerta_id: int, organos: list[int], regiones: list[int], usuario_id: int
) -> None:
    db.add_all(
        AlertaOrgano(alerta_id=alerta_id, organo_bdns_id=id_bdns, created_by=usuario_id, updated_by=usuario_id)
        for id_bdns in organos
    )
    db.add_all(
        AlertaRegion(alerta_id=alerta_id, region_bdns_id=id_bdns, created_by=usuario_id, updated_by=usuario_id)
        for id_bdns in regiones
    )


async def _leer(db: AsyncSession, alerta_id: int) -> AlertaRead:
    """Relee la alerta con sus filtros resueltos.

    `populate_existing` porque la alerta puede seguir en el identity map de
    la sesión con el updated_at anterior al UPDATE explícito.
    """
    alerta = (
        await db.execute(
            select(Alerta).where(Alerta.id == alerta_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    # Ordenados por id de fila: se devuelven en el orden en que se enviaron.
    organos = await db.scalars(
        select(AlertaOrgano.organo_bdns_id).where(AlertaOrgano.alerta_id == alerta_id).order_by(AlertaOrgano.id)
    )
    regiones = await db.scalars(
        select(AlertaRegion.region_bdns_id).where(AlertaRegion.alerta_id == alerta_id).order_by(AlertaRegion.id)
    )

    columnas = {
        campo: getattr(alerta, campo) for campo in AlertaRead.model_fields if campo not in ("organos", "regiones")
    }
    return AlertaRead(**columnas, organos=list(organos), regiones=list(regiones))
