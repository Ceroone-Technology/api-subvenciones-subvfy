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
hace upsert: si ya estaba cacheada se refresca, y si no, se crea. Sin esto,
la FK `favorito.convocatoria_id` no tendría a qué apuntar.
"""

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql

from app.api.deps import DbDep, PaginacionDep, UsuarioActualDep
from app.core.permisos import UsuarioAutenticado
from app.models import Convocatoria, Favorito
from app.schemas.common import Pagina
from app.schemas.convocatoria import ConvocatoriaUpsert
from app.schemas.favorito import FavoritoCreate, FavoritoRead, FavoritoUpdate

router = APIRouter(prefix="/favoritos", tags=["favoritos"])

_NO_ENCONTRADO = HTTPException(
    status.HTTP_404_NOT_FOUND, detail="No tienes esa convocatoria en favoritos."
)


async def _cachear_convocatoria(db: DbDep, datos: ConvocatoriaUpsert, usuario_id: int) -> int:
    """Upsert por `codigo_bdns`, devolviendo el id local.

    Solo se sobrescriben los campos que el cliente ha enviado
    (`exclude_unset`): marcar un favorito desde el listado de resultados,
    donde la ficha viene incompleta, no puede borrar los datos que una
    sincronización anterior ya había guardado.
    """
    valores = datos.model_dump(exclude_unset=True)
    actualizables = {
        campo: valor for campo, valor in valores.items() if campo != "codigo_bdns" and valor is not None
    }

    insercion = insert_postgresql(Convocatoria).values(
        **valores, created_by=usuario_id, updated_by=usuario_id
    )
    sentencia = insercion.on_conflict_do_update(
        index_elements=[Convocatoria.codigo_bdns],
        set_={
            **actualizables,
            "sincronizado_at": func.now(),
            "updated_at": func.now(),
            "updated_by": usuario_id,
        },
    ).returning(Convocatoria.id)
    return (await db.execute(sentencia)).scalar_one()


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


async def _leer(db: DbDep, favorito_id: int) -> FavoritoRead:
    """Relee el favorito con su convocatoria para devolverlo resuelto."""
    favorito, convocatoria = (
        await db.execute(
            select(Favorito, Convocatoria)
            .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
            .where(Favorito.id == favorito_id)
        )
    ).one()
    return FavoritoRead(
        id=favorito.id,
        nota=favorito.nota,
        created_at=favorito.created_at,
        updated_at=favorito.updated_at,
        convocatoria=convocatoria,
    )


@router.get("", response_model=Pagina[FavoritoRead], summary="Listar mis favoritos")
async def listar_favoritos(
    db: DbDep,
    paginacion: PaginacionDep,
    actual: UsuarioActualDep,
    q: str | None = Query(default=None, description="Busca en el título y el código BDNS."),
) -> Pagina[FavoritoRead]:
    filtros = [Favorito.usuario_id == actual.id]
    if q:
        patron = f"%{q.strip()}%"
        filtros.append(
            or_(Convocatoria.titulo.ilike(patron), Convocatoria.codigo_bdns.ilike(patron))
        )

    base = select(Favorito).join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id).where(*filtros)
    total = await db.scalar(
        select(func.count())
        .select_from(Favorito)
        .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
        .where(*filtros)
    )
    filas = (
        await db.execute(
            base.add_columns(Convocatoria)
            # Lo último marcado, primero: es el orden con el que se usa una
            # lista de favoritos.
            .order_by(Favorito.created_at.desc(), Favorito.id.desc())
            .offset(paginacion.offset)
            .limit(paginacion.size)
        )
    ).all()

    return Pagina[FavoritoRead](
        items=[
            FavoritoRead(
                id=favorito.id,
                nota=favorito.nota,
                created_at=favorito.created_at,
                updated_at=favorito.updated_at,
                convocatoria=convocatoria,
            )
            for favorito, convocatoria in filas
        ],
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
