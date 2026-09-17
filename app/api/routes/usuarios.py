"""CRUD de usuarios.

Permisos (ver `app.core.permisos` para el modelo completo):

| Acción             | admin | gestor              | usuario                  |
|--------------------|-------|---------------------|--------------------------|
| Listar / consultar | todos | los de su empresa   | los de su empresa        |
| Crear              | sí    | en su empresa       | no                       |
| Editar             | sí    | los de su empresa   | solo su propio perfil    |
| Dar de baja        | sí    | los de su empresa   | no                       |

Un `usuario` editando su propio perfil no puede tocar `empresa_id`,
`rol_id` ni `estado`: son justo los campos con los que se ascendería a sí
mismo o se cambiaría de tenant.

Dos reglas propias de este recurso:

- La contraseña entra en claro y se guarda hasheada (`app.core.security`);
  `password_hash` no se expone nunca en las respuestas.
- `DELETE /usuarios/{id}` es baja lógica (`estado = "inactivo"`): un usuario
  aparece como `created_by`/`updated_by` en las filas que generó, así que
  borrarlo físicamente rompería la traza de auditoría.
"""

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import DbDep, GestorDep, PaginacionDep, UsuarioActualDep
from app.core.permisos import ROL_ADMIN, ROL_GESTOR, UsuarioAutenticado
from app.core.security import hashear_password
from app.models import Empresa, Rol, Usuario
from app.schemas.common import Pagina
from app.schemas.usuario import EstadoUsuario, UsuarioCreate, UsuarioRead, UsuarioUpdate

router = APIRouter(prefix="/usuarios", tags=["usuarios"])

# Campos que un usuario sin rol de gestión no puede cambiarse a sí mismo.
CAMPOS_DE_GESTION = ("empresa_id", "rol_id", "estado")

_NO_ENCONTRADO = HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")


async def _obtener_visible_o_404(db: DbDep, usuario_id: int, actual: UsuarioAutenticado) -> Usuario:
    """404 también cuando el usuario existe pero es de otra empresa: a un
    cliente no se le confirma qué ids están ocupados en otro tenant."""
    usuario = await db.get(Usuario, usuario_id)
    if usuario is None or not actual.puede_ver_empresa(usuario.empresa_id):
        raise _NO_ENCONTRADO
    return usuario


async def _exigir_email_libre(db: DbDep, email: str, excluir_id: int | None = None) -> None:
    consulta = select(Usuario.id).where(Usuario.email == email)
    if excluir_id is not None:
        consulta = consulta.where(Usuario.id != excluir_id)
    if (await db.execute(consulta)).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Ya existe un usuario con el email {email}.")


async def _exigir_referencias_validas(db: DbDep, empresa_id: int | None, rol_id: int | None) -> None:
    """400 en vez de dejar que asyncpg devuelva un ForeignKeyViolationError:
    el cliente ha mandado datos mal, no es un fallo del servidor."""
    if empresa_id is not None and await db.get(Empresa, empresa_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"No existe la empresa {empresa_id}.")
    if rol_id is not None and await db.get(Rol, rol_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"No existe el rol {rol_id}.")


@router.get("", response_model=Pagina[UsuarioRead], summary="Listar usuarios")
async def listar_usuarios(
    db: DbDep,
    paginacion: PaginacionDep,
    actual: UsuarioActualDep,
    q: str | None = Query(default=None, description="Busca en nombre, apellidos y email."),
    empresa_id: int | None = Query(default=None, description="Solo admin puede consultar otra empresa."),
    rol_id: int | None = Query(default=None),
    estado: EstadoUsuario | None = Query(default=None),
) -> Pagina[UsuarioRead]:
    filtros = []
    # El filtro por empresa se impone, no se negocia: si no eres admin, el
    # empresa_id que hayas pedido se ignora a favor del tuyo.
    empresa_efectiva = actual.filtro_empresa if actual.filtro_empresa is not None else empresa_id
    if empresa_efectiva is not None:
        filtros.append(Usuario.empresa_id == empresa_efectiva)
    if q:
        patron = f"%{q.strip()}%"
        filtros.append(
            or_(
                Usuario.nombre.ilike(patron),
                Usuario.apellidos.ilike(patron),
                Usuario.email.ilike(patron),
            )
        )
    if rol_id is not None:
        filtros.append(Usuario.rol_id == rol_id)
    if estado:
        filtros.append(Usuario.estado == estado)

    total = await db.scalar(select(func.count()).select_from(Usuario).where(*filtros))
    result = await db.execute(
        select(Usuario)
        .where(*filtros)
        .order_by(Usuario.apellidos, Usuario.nombre)
        .offset(paginacion.offset)
        .limit(paginacion.size)
    )
    return Pagina[UsuarioRead](
        items=[UsuarioRead.model_validate(u) for u in result.scalars().all()],
        total=total or 0,
        page=paginacion.page,
        size=paginacion.size,
    )


@router.post("", response_model=UsuarioRead, status_code=status.HTTP_201_CREATED, summary="Crear usuario")
async def crear_usuario(datos: UsuarioCreate, db: DbDep, actual: GestorDep) -> Usuario:
    # Un gestor da de alta en su empresa; llevar gente a otra es cosa de admin.
    actual.exigir_acceso_a_empresa(datos.empresa_id)
    await _exigir_referencias_validas(db, datos.empresa_id, datos.rol_id)
    await _exigir_email_libre(db, datos.email)

    campos = datos.model_dump(exclude={"password"})
    usuario = Usuario(
        **campos,
        password_hash=hashear_password(datos.password),
        created_by=actual.id,
        updated_by=actual.id,
    )
    db.add(usuario)
    await db.commit()
    await db.refresh(usuario)
    return usuario


@router.get("/{usuario_id}", response_model=UsuarioRead, summary="Obtener un usuario")
async def obtener_usuario(usuario_id: int, db: DbDep, actual: UsuarioActualDep) -> Usuario:
    return await _obtener_visible_o_404(db, usuario_id, actual)


@router.patch("/{usuario_id}", response_model=UsuarioRead, summary="Actualizar un usuario")
async def actualizar_usuario(
    usuario_id: int, datos: UsuarioUpdate, db: DbDep, actual: UsuarioActualDep
) -> Usuario:
    usuario = await _obtener_visible_o_404(db, usuario_id, actual)
    es_gestion = actual.rol in (ROL_ADMIN, ROL_GESTOR)
    if not es_gestion and usuario.id != actual.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Solo puedes editar tu propio perfil.")

    cambios = datos.model_dump(exclude_unset=True)
    if not es_gestion:
        prohibidos = sorted(set(cambios) & set(CAMPOS_DE_GESTION))
        if prohibidos:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"No puedes modificar estos campos de tu perfil: {', '.join(prohibidos)}.",
            )

    if "empresa_id" in cambios:
        actual.exigir_acceso_a_empresa(cambios["empresa_id"])
    await _exigir_referencias_validas(db, cambios.get("empresa_id"), cambios.get("rol_id"))
    if "email" in cambios:
        await _exigir_email_libre(db, cambios["email"], excluir_id=usuario_id)
    if "password" in cambios:
        usuario.password_hash = hashear_password(cambios.pop("password"))

    for campo, valor in cambios.items():
        setattr(usuario, campo, valor)
    usuario.updated_by = actual.id
    await db.commit()
    await db.refresh(usuario)
    return usuario


@router.delete("/{usuario_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Dar de baja un usuario")
async def desactivar_usuario(usuario_id: int, db: DbDep, actual: GestorDep) -> None:
    usuario = await _obtener_visible_o_404(db, usuario_id, actual)
    if usuario.id == actual.id:
        # Evita que el único admin de una instalación se deje fuera.
        raise HTTPException(status.HTTP_409_CONFLICT, detail="No puedes darte de baja a ti mismo.")
    usuario.estado = "inactivo"
    usuario.updated_by = actual.id
    await db.commit()
