"""Favoritos del usuario (Hito 3, Funcionalidad 1).

**Los favoritos son personales, no de la empresa.** La tabla cuelga de
`usuario_id`, y aquí el filtro es siempre el usuario del token: ni un gestor
ni un admin ven los favoritos de otra persona. Es lo que espera cualquiera
que marca una estrella en un buscador, y evita de paso la pregunta
incómoda de si tu jefe ve lo que estabas mirando.

**Se direccionan por `codigo_bdns`, no por el id de la fila.** El frontend
consulta la BDNS directamente: lo que tiene en la mano al pintar el botón
de favorito es el código de la convocatoria, no un id nuestro. Con
`DELETE /favoritos/{codigo_bdns}` el botón funciona sin tener que
arrastrar ningún identificador interno.

**Marcar un favorito siembra la caché.** `convocatoria` es una copia local
de la BDNS (no la fuente de verdad), así que el POST trae la ficha dentro y
la siembra solo si no estaba cacheada (nunca sobrescribe: la tabla es de todos). Sin esto,
la FK `favorito.convocatoria_id` no tendría a qué apuntar.
"""

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql

from app.api.deps import DbDep, PaginacionDep, UsuarioActualDep
from app.core.permisos import UsuarioAutenticado
from app.models import Convocatoria, Favorito
from app.schemas.common import Pagina
from app.schemas.convocatoria import ConvocatoriaRead, ConvocatoriaUpsert
from app.schemas.favorito import (
    ConvocatoriaFavorita,
    FavoritoCreate,
    FavoritoRead,
    FavoritosResumen,
    FavoritoUpdate,
    PorPlazo,
)
from app.services.convocatoria_estado import estado_expr, hoy_madrid

router = APIRouter(prefix="/favoritos", tags=["favoritos"])

_NO_ENCONTRADO = HTTPException(
    status.HTTP_404_NOT_FOUND, detail="No tienes esa convocatoria en favoritos."
)


async def _cachear_convocatoria(db: DbDep, datos: ConvocatoriaUpsert, usuario_id: int) -> int:
    """Id local de la convocatoria; la siembra solo si aún no está cacheada.

    **La tabla es compartida por todas las empresas**, así que un cliente nunca
    sobrescribe una fila existente: `ON CONFLICT DO NOTHING`, como el tablero
    de seguimiento. La fila sembrada es un marcador de posición: sin
    `url_portal_oficial` (la sincronización nunca lo rellena y un enlace del
    cliente se quedaría para siempre) y sin `detalle_sync_at`, de modo que la
    sincronización la completa y reescribe el título con el dato oficial.
    """
    valores = datos.model_dump(exclude_unset=True, exclude={"url_portal_oficial"})
    await db.execute(
        insert_postgresql(Convocatoria)
        .values(**valores, created_by=usuario_id, updated_by=usuario_id)
        .on_conflict_do_nothing(index_elements=[Convocatoria.codigo_bdns])
    )
    return (
        await db.execute(select(Convocatoria.id).where(Convocatoria.codigo_bdns == datos.codigo_bdns))
    ).scalar_one()


async def _obtener_favorito_o_404(
    db: DbDep, codigo_bdns: str, actual: UsuarioAutenticado
) -> Favorito:
    favorito = (
        await db.execute(
            select(Favorito)
            .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
            .where(Favorito.usuario_id == actual.id, Convocatoria.codigo_bdns == codigo_bdns)
        )
    ).scalar_one_or_none()
    if favorito is None:
        raise _NO_ENCONTRADO
    return favorito


_ESTADO = estado_expr().label("estado")
_DIAS = (Convocatoria.fecha_fin_solicitud - hoy_madrid()).label("dias_restantes")


def _a_read(favorito: Favorito, convocatoria: Convocatoria, estado: str, dias: int | None) -> FavoritoRead:
    ficha = ConvocatoriaRead.model_validate(convocatoria).model_dump()
    ficha |= {
        "fecha_inicio_solicitud": convocatoria.fecha_inicio_solicitud,
        "fecha_fin_solicitud": convocatoria.fecha_fin_solicitud,
        "estado": estado,
        "plazo_permanente": convocatoria.plazo_permanente,
        "presupuesto_total": convocatoria.presupuesto_total,
        "tema": convocatoria.tema,
        "gestiona_subvfy": convocatoria.gestiona_subvfy,
        "dias_restantes": dias,
    }
    return FavoritoRead(
        id=favorito.id,
        nota=favorito.nota,
        created_at=favorito.created_at,
        updated_at=favorito.updated_at,
        convocatoria=ConvocatoriaFavorita.model_validate(ficha),
    )


async def _leer(db: DbDep, favorito_id: int) -> FavoritoRead:
    """Relee el favorito con su convocatoria para devolverlo resuelto."""
    favorito, convocatoria, estado, dias = (
        await db.execute(
            select(Favorito, Convocatoria, _ESTADO, _DIAS)
            .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
            .where(Favorito.id == favorito_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return _a_read(favorito, convocatoria, estado, dias)


@router.get("/resumen", response_model=FavoritosResumen, summary="Resumen de mis favoritos")
async def resumen_favoritos(db: DbDep, actual: UsuarioActualDep) -> FavoritosResumen:
    """Guardadas, importe sumado y barra por plazo, en una sola consulta (las cifras de la cabecera de Favoritos)."""
    dias = Convocatoria.fecha_fin_solicitud - hoy_madrid()
    fila = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Convocatoria.presupuesto_total), 0),
                func.count().filter(dias.between(0, 14)),
                func.count().filter(func.coalesce(func.btrim(Favorito.nota), "") != ""),
                func.count().filter(dias < 0),
                func.count().filter(dias.between(0, 7)),
                func.count().filter(dias.between(8, 30)),
                func.count().filter(dias > 30),
                func.count().filter(Convocatoria.fecha_fin_solicitud.is_(None)),
            )
            .select_from(Favorito)
            .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
            .where(Favorito.usuario_id == actual.id)
        )
    ).one()
    return FavoritosResumen(
        total=fila[0],
        importe_total=float(fila[1]),
        cierran_en_14_dias=fila[2],
        con_nota=fila[3],
        por_plazo=PorPlazo(
            vencidas=fila[4], hasta_7_dias=fila[5], de_8_a_30_dias=fila[6], mas_de_30_dias=fila[7], sin_fecha=fila[8]
        ),
    )


@router.get("", response_model=Pagina[FavoritoRead], summary="Listar mis favoritos")
async def listar_favoritos(
    db: DbDep,
    paginacion: PaginacionDep,
    actual: UsuarioActualDep,
    q: str | None = Query(default=None, description="Busca en el título y el código BDNS."),
    cierran_en_dias: int | None = Query(
        default=None, ge=0, le=365, description="Solo las que cierran de hoy a dentro de N días (inclusive)."
    ),
    con_nota: bool | None = Query(default=None, description="`true`: solo con nota. `false`: solo sin nota."),
) -> Pagina[FavoritoRead]:
    filtros = [Favorito.usuario_id == actual.id]
    if q:
        patron = f"%{q.strip()}%"
        filtros.append(
            or_(Convocatoria.titulo.ilike(patron), Convocatoria.codigo_bdns.ilike(patron))
        )
    if cierran_en_dias is not None:
        filtros.append((Convocatoria.fecha_fin_solicitud - hoy_madrid()).between(0, cierran_en_dias))
    if con_nota is not None:
        hay_nota = func.coalesce(func.btrim(Favorito.nota), "") != ""
        filtros.append(hay_nota if con_nota else ~hay_nota)

    total = await db.scalar(
        select(func.count())
        .select_from(Favorito)
        .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
        .where(*filtros)
    )
    filas = (
        await db.execute(
            select(Favorito, Convocatoria, _ESTADO, _DIAS)
            .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
            .where(*filtros)
            # Lo último marcado, primero: es el orden con el que se usa una
            # lista de favoritos.
            .order_by(Favorito.created_at.desc(), Favorito.id.desc())
            .offset(paginacion.offset)
            .limit(paginacion.size)
        )
    ).all()

    return Pagina[FavoritoRead](
        items=[_a_read(favorito, convocatoria, estado, dias) for favorito, convocatoria, estado, dias in filas],
        total=total or 0,
        page=paginacion.page,
        size=paginacion.size,
    )


@router.post("", response_model=FavoritoRead, status_code=status.HTTP_201_CREATED, summary="Marcar favorito")
async def marcar_favorito(datos: FavoritoCreate, db: DbDep, actual: UsuarioActualDep) -> FavoritoRead:
    convocatoria_id = await _cachear_convocatoria(db, datos.convocatoria, actual.id)

    ya_marcado = (
        await db.execute(
            select(Favorito.id).where(
                Favorito.usuario_id == actual.id, Favorito.convocatoria_id == convocatoria_id
            )
        )
    ).first()
    if ya_marcado is not None:
        # 409 y no un 200 idempotente: así el frontend distingue "lo acabo de
        # marcar" de "ya lo tenía", que es justo lo que necesita saber para
        # no duplicar un aviso al usuario.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"La convocatoria {datos.convocatoria.codigo_bdns} ya está en tus favoritos.",
        )

    favorito = Favorito(
        usuario_id=actual.id,
        convocatoria_id=convocatoria_id,
        nota=datos.nota,
        created_by=actual.id,
        updated_by=actual.id,
    )
    db.add(favorito)
    await db.commit()
    return await _leer(db, favorito.id)


@router.get("/{codigo_bdns}", response_model=FavoritoRead, summary="Consultar un favorito")
async def obtener_favorito(codigo_bdns: str, db: DbDep, actual: UsuarioActualDep) -> FavoritoRead:
    """404 cuando no está marcada: es también la forma de preguntar "¿tengo
    esta convocatoria en favoritos?" desde la ficha de detalle."""
    favorito = await _obtener_favorito_o_404(db, codigo_bdns, actual)
    return await _leer(db, favorito.id)


@router.patch("/{codigo_bdns}", response_model=FavoritoRead, summary="Editar la nota de un favorito")
async def actualizar_nota(
    codigo_bdns: str, datos: FavoritoUpdate, db: DbDep, actual: UsuarioActualDep
) -> FavoritoRead:
    favorito = await _obtener_favorito_o_404(db, codigo_bdns, actual)
    cambios = datos.model_dump(exclude_unset=True)
    for campo, valor in cambios.items():
        setattr(favorito, campo, valor)
    favorito.updated_by = actual.id
    await db.commit()
    return await _leer(db, favorito.id)


@router.delete("/{codigo_bdns}", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar de favoritos")
async def quitar_favorito(codigo_bdns: str, db: DbDep, actual: UsuarioActualDep) -> None:
    """Borrado físico, al contrario que en empresa y usuario: un favorito no
    tiene valor histórico ni nada que lo referencie, y la convocatoria
    cacheada se queda donde está."""
    favorito = await _obtener_favorito_o_404(db, codigo_bdns, actual)
    await db.delete(favorito)
    await db.commit()
