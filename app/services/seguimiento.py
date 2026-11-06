"""Follow-up board of a company (plan N5a/N5b).

Scope is the company of the token, **admin included**: the board belongs to the tenant, not to the platform, so a
card of another company is a 404 for everyone. Roles (plan section 4): admin and gestor manage everything; a
`usuario` sees every card but only moves and edits the ones assigned to them, and can take an unassigned one.
Order of checks: tenant first (404), then role (403).
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permisos import ROL_ADMIN, ROL_GESTOR, UsuarioAutenticado
from app.models import Convocatoria, Seguimiento, SeguimientoEvento, SeguimientoTarea, Usuario
from app.models.seguimiento import ESTADOS_SEGUIMIENTO
from app.schemas.seguimiento import (
    ResumenConcedidas,
    ResumenEstado,
    SeguimientoCreate,
    SeguimientoRead,
    SeguimientoResumen,
    SeguimientoUpdate,
    TareaCreate,
    TareaRead,
    TareaUpdate,
)

MAX_TARJETAS = 500
ZONA = ZoneInfo("Europe/Madrid")

_NO_ENCONTRADO = HTTPException(status.HTTP_404_NOT_FOUND, detail="Seguimiento no encontrado.")
_TAREA_NO_ENCONTRADA = HTTPException(status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada.")


def _gestiona(actual: UsuarioAutenticado) -> bool:
    return actual.rol in (ROL_ADMIN, ROL_GESTOR)


def _hoy() -> date:
    return datetime.now(ZONA).date()


def _prohibido(detalle: str) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail=detalle)


async def _comprobar_persona(db: AsyncSession, actual: UsuarioAutenticado, usuario_id: int) -> None:
    """The person must be an active user of the same company (400, like every bad reference of the API)."""
    valido = await db.scalar(
        select(Usuario.id).where(
            Usuario.id == usuario_id, Usuario.empresa_id == actual.empresa_id, Usuario.estado == "activo"
        )
    )
    if valido is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="La persona indicada no es un usuario activo de tu empresa."
        )


async def _tarjetas(
    db: AsyncSession,
    empresa_id: int,
    *,
    seguimiento_id: int | None = None,
    estado: str | None = None,
    responsable_id: int | None = None,
) -> list[SeguimientoRead]:
    """Cards with their tasks: two queries however many cards there are (no lazy loads)."""
    consulta = (
        select(Seguimiento, Convocatoria)
        .join(Convocatoria, Convocatoria.id == Seguimiento.convocatoria_id)
        .where(Seguimiento.empresa_id == empresa_id)
    )
    if seguimiento_id is not None:
        consulta = consulta.where(Seguimiento.id == seguimiento_id)
    if estado is not None:
        consulta = consulta.where(Seguimiento.estado == estado)
    if responsable_id is not None:
        consulta = consulta.where(Seguimiento.responsable_usuario_id == responsable_id)
    filas = (
        await db.execute(
            consulta.order_by(Convocatoria.fecha_fin_solicitud.asc().nulls_last(), Seguimiento.id)
            .limit(MAX_TARJETAS)
            .execution_options(populate_existing=True)
        )
    ).all()
    ids = [s.id for s, _ in filas]
    tareas: dict[int, list[SeguimientoTarea]] = {i: [] for i in ids}
    if ids:
        for t in await db.scalars(
            select(SeguimientoTarea)
            .where(SeguimientoTarea.seguimiento_id.in_(ids))
            .order_by(SeguimientoTarea.id)
            .execution_options(populate_existing=True)
        ):
            tareas[t.seguimiento_id].append(t)
    return [
        SeguimientoRead(
            id=s.id,
            codigo_bdns=c.codigo_bdns,
            titulo=c.titulo,
            organo=c.organo_convocante,
            tema=c.tema,
            importe=float(c.presupuesto_total) if c.presupuesto_total is not None else None,
            fecha_fin_solicitud=c.fecha_fin_solicitud,
            estado=s.estado,
            resultado=s.resultado,
            responsable_id=s.responsable_usuario_id,
            fecha_presentacion=s.fecha_presentacion,
            tareas=[TareaRead.model_validate(t) for t in tareas[s.id]],
            tareas_hechas=sum(1 for t in tareas[s.id] if t.hecha),
            tareas_total=len(tareas[s.id]),
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s, c in filas
    ]


async def listar(
    db: AsyncSession, actual: UsuarioAutenticado, *, estado: str | None = None, responsable_id: int | None = None
) -> list[SeguimientoRead]:
    return await _tarjetas(db, actual.empresa_id, estado=estado, responsable_id=responsable_id)


async def leer(db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int) -> SeguimientoRead:
    tarjetas = await _tarjetas(db, actual.empresa_id, seguimiento_id=seguimiento_id)
    if not tarjetas:
        raise _NO_ENCONTRADO
    return tarjetas[0]


async def _cargar(db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int) -> Seguimiento:
    seg = await db.scalar(
        select(Seguimiento)
        .where(Seguimiento.id == seguimiento_id, Seguimiento.empresa_id == actual.empresa_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if seg is None:
        raise _NO_ENCONTRADO
    return seg


def _evento(
    seg: Seguimiento, actual: UsuarioAutenticado, tipo: str, anterior: str | None = None, nuevo: str | None = None
) -> SeguimientoEvento:
    return SeguimientoEvento(
        seguimiento_id=seg.id,
        usuario_id=actual.id,
        tipo=tipo,
        estado_anterior=anterior,
        estado_nuevo=nuevo,
        created_by=actual.id,
        updated_by=actual.id,
    )


async def _id_convocatoria(db: AsyncSession, datos: SeguimientoCreate, actual: UsuarioAutenticado) -> int:
    existente = await db.scalar(select(Convocatoria.id).where(Convocatoria.codigo_bdns == datos.codigo_bdns))
    if existente is not None:
        return existente  # the synced copy wins: the request only seeds a call that is not cached yet
    valores: dict[str, object] = {
        "codigo_bdns": datos.codigo_bdns,
        "titulo": datos.titulo,
        "created_by": actual.id,
        "updated_by": actual.id,
    }
    opcionales = (
        ("organo_convocante", datos.organo),
        ("fecha_fin_solicitud", datos.fecha_fin_solicitud),
        ("presupuesto_total", None if datos.importe is None else Decimal(str(datos.importe))),
        ("tema", datos.tema),
    )
    for campo, valor in opcionales:
        if valor is not None:
            valores[campo] = valor
    await db.execute(
        insert_postgresql(Convocatoria).values(**valores).on_conflict_do_nothing(index_elements=["codigo_bdns"])
    )
    return (await db.execute(select(Convocatoria.id).where(Convocatoria.codigo_bdns == datos.codigo_bdns))).scalar_one()


async def crear(
    db: AsyncSession, actual: UsuarioAutenticado, datos: SeguimientoCreate
) -> tuple[SeguimientoRead, bool]:
    """Adds a call to the board. Returns the card and whether it is new (False = it was already there)."""
    convocatoria_id = await _id_convocatoria(db, datos, actual)
    nuevo_id = (
        await db.execute(
            insert_postgresql(Seguimiento)
            .values(
                empresa_id=actual.empresa_id,
                convocatoria_id=convocatoria_id,
                created_by=actual.id,
                updated_by=actual.id,
            )
            .on_conflict_do_nothing(constraint="uq_seguimiento_empresa_convocatoria")
            .returning(Seguimiento.id)
        )
    ).scalar_one_or_none()
    if nuevo_id is None:
        await db.commit()  # keeps a call seeded just above
        existente = (
            await db.execute(
                select(Seguimiento.id).where(
                    Seguimiento.empresa_id == actual.empresa_id, Seguimiento.convocatoria_id == convocatoria_id
                )
            )
        ).scalar_one()
        return await leer(db, actual, existente), False
    db.add(
        SeguimientoEvento(
            seguimiento_id=nuevo_id,
            usuario_id=actual.id,
            tipo="creada",
            estado_nuevo="interesa",
            created_by=actual.id,
            updated_by=actual.id,
        )
    )
    await db.commit()
    return await leer(db, actual, nuevo_id), True


async def actualizar(
    db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int, datos: SeguimientoUpdate
) -> SeguimientoRead:
    seg = await _cargar(db, actual, seguimiento_id)
    campos = datos.model_fields_set
    if "estado" in campos and datos.estado is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="estado no admite null.")
    gestiona = _gestiona(actual)

    # A usuario may take an unassigned card (also in the same request that moves it).
    se_autoasigna = "responsable_id" in campos and datos.responsable_id == actual.id
    if "responsable_id" in campos and not gestiona:
        if not se_autoasigna or seg.responsable_usuario_id not in (None, actual.id):
            raise _prohibido("Solo puedes asignarte una tarjeta que no tenga responsable.")
    sera_mia = seg.responsable_usuario_id == actual.id or se_autoasigna
    if "estado" in campos and not (gestiona or sera_mia):
        raise _prohibido("Solo puedes mover las tarjetas asignadas a ti.")
    if not gestiona and ({"resultado", "fecha_presentacion"} & campos):
        raise _prohibido("El resultado y la fecha de presentación los registra un gestor.")

    anterior = seg.estado
    nuevo_estado = datos.estado if "estado" in campos and datos.estado is not None else anterior
    if datos.resultado is not None and nuevo_estado != "resuelta":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="El resultado solo se registra en una tarjeta Resuelta."
        )
    if "responsable_id" in campos and datos.responsable_id is not None and gestiona:
        await _comprobar_persona(db, actual, datos.responsable_id)

    eventos: list[SeguimientoEvento] = []
    if nuevo_estado != anterior:
        seg.estado = nuevo_estado
        eventos.append(_evento(seg, actual, "movida", anterior, nuevo_estado))
        if nuevo_estado != "resuelta":
            seg.resultado = None  # leaving Resuelta forgets the outcome
        if nuevo_estado == "presentada" and seg.fecha_presentacion is None and "fecha_presentacion" not in campos:
            seg.fecha_presentacion = _hoy()
    if "resultado" in campos and datos.resultado != seg.resultado:
        seg.resultado = datos.resultado
        if datos.resultado is not None:
            eventos.append(_evento(seg, actual, "resuelta", nuevo=datos.resultado))
    if "fecha_presentacion" in campos:
        seg.fecha_presentacion = datos.fecha_presentacion
    if "responsable_id" in campos and datos.responsable_id != seg.responsable_usuario_id:
        seg.responsable_usuario_id = datos.responsable_id
        eventos.append(_evento(seg, actual, "asignada"))

    seg.updated_by = actual.id
    db.add_all(eventos)
    await db.commit()
    return await leer(db, actual, seguimiento_id)


async def eliminar(db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int) -> None:
    seg = await _cargar(db, actual, seguimiento_id)
    if not _gestiona(actual):
        raise _prohibido("Solo un gestor puede quitar una tarjeta del seguimiento.")
    await db.delete(seg)
    await db.commit()


async def resumen(db: AsyncSession, actual: UsuarioAutenticado) -> SeguimientoResumen:
    importe = func.coalesce(func.sum(Convocatoria.presupuesto_total), 0)
    base = (
        select(Seguimiento.estado, func.count(), importe)
        .join(Convocatoria, Convocatoria.id == Seguimiento.convocatoria_id)
        .where(Seguimiento.empresa_id == actual.empresa_id)
    )
    por_estado = {e: (n, float(i)) for e, n, i in (await db.execute(base.group_by(Seguimiento.estado))).all()}
    total_c, importe_c = (
        await db.execute(
            select(func.count(), importe)
            .select_from(Seguimiento)
            .join(Convocatoria, Convocatoria.id == Seguimiento.convocatoria_id)
            .where(Seguimiento.empresa_id == actual.empresa_id, Seguimiento.resultado == "concedida")
        )
    ).one()
    estados = [
        ResumenEstado(estado=e, total=por_estado.get(e, (0, 0.0))[0], importe=por_estado.get(e, (0, 0.0))[1])
        for e in ESTADOS_SEGUIMIENTO
    ]
    return SeguimientoResumen(
        estados=estados,
        concedidas=ResumenConcedidas(total=total_c, importe=float(importe_c)),
        total=sum(e.total for e in estados),
        importe_total=sum(e.importe for e in estados),
    )


async def eventos(db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int) -> list[SeguimientoEvento]:
    await leer(db, actual, seguimiento_id)  # tenant check (404)
    return list(
        await db.scalars(
            select(SeguimientoEvento)
            .where(SeguimientoEvento.seguimiento_id == seguimiento_id)
            .order_by(SeguimientoEvento.ocurrido_at.desc(), SeguimientoEvento.id.desc())
        )
    )


# --- Tasks (N5b) -----------------------------------------------------------------------------------------------


def _exigir_editar_tareas(actual: UsuarioAutenticado, seg: Seguimiento) -> None:
    if not (_gestiona(actual) or seg.responsable_usuario_id == actual.id):
        raise _prohibido("Solo puedes editar las tareas de las tarjetas asignadas a ti.")


async def _comprobar_asignado(db: AsyncSession, actual: UsuarioAutenticado, asignado_a: int | None) -> None:
    if asignado_a is None:
        return
    if not _gestiona(actual) and asignado_a != actual.id:
        raise _prohibido("Solo puedes asignarte tareas a ti mismo.")
    await _comprobar_persona(db, actual, asignado_a)


async def crear_tarea(
    db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int, datos: TareaCreate
) -> SeguimientoRead:
    seg = await _cargar(db, actual, seguimiento_id)
    _exigir_editar_tareas(actual, seg)
    await _comprobar_asignado(db, actual, datos.asignado_a)
    db.add(
        SeguimientoTarea(
            seguimiento_id=seg.id,
            titulo=datos.titulo.strip(),
            tipo=datos.tipo,
            fecha_limite=datos.fecha_limite,
            asignado_a=datos.asignado_a,
            created_by=actual.id,
            updated_by=actual.id,
        )
    )
    await db.commit()
    return await leer(db, actual, seguimiento_id)


async def _cargar_tarea(db: AsyncSession, seg: Seguimiento, tarea_id: int) -> SeguimientoTarea:
    tarea = await db.scalar(
        select(SeguimientoTarea)
        .where(SeguimientoTarea.id == tarea_id, SeguimientoTarea.seguimiento_id == seg.id)
        .execution_options(populate_existing=True)
    )
    if tarea is None:
        raise _TAREA_NO_ENCONTRADA
    return tarea


async def actualizar_tarea(
    db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int, tarea_id: int, datos: TareaUpdate
) -> SeguimientoRead:
    seg = await _cargar(db, actual, seguimiento_id)
    tarea = await _cargar_tarea(db, seg, tarea_id)
    _exigir_editar_tareas(actual, seg)
    campos = datos.model_fields_set
    if ("hecha" in campos and datos.hecha is None) or ("titulo" in campos and datos.titulo is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="titulo y hecha no admiten null.")
    if "asignado_a" in campos:
        await _comprobar_asignado(db, actual, datos.asignado_a)
        tarea.asignado_a = datos.asignado_a
    if datos.titulo is not None:
        tarea.titulo = datos.titulo.strip()
    if datos.hecha is not None:
        tarea.hecha = datos.hecha
    if "fecha_limite" in campos:
        tarea.fecha_limite = datos.fecha_limite
    tarea.updated_by = actual.id
    await db.commit()
    return await leer(db, actual, seguimiento_id)


async def eliminar_tarea(
    db: AsyncSession, actual: UsuarioAutenticado, seguimiento_id: int, tarea_id: int
) -> SeguimientoRead:
    seg = await _cargar(db, actual, seguimiento_id)
    tarea = await _cargar_tarea(db, seg, tarea_id)
    _exigir_editar_tareas(actual, seg)
    await db.delete(tarea)
    await db.commit()
    return await leer(db, actual, seguimiento_id)
