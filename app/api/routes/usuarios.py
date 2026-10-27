"""CRUD de usuarios.

Permisos (ver `app.core.permisos` para el modelo completo):

| Acción             | admin | gestor                              | usuario               |
|--------------------|-------|-------------------------------------|-----------------------|
| Listar / consultar | todos | los de su empresa                   | los de su empresa     |
| Crear              | sí    | en su empresa, con rol `usuario`    | no                    |
| Editar             | sí    | los de rol `usuario` de su empresa  | solo su propio perfil |
| Dar de baja        | sí    | los de rol `usuario` de su empresa  | no                    |

Tres reglas que cierran la escalada que encontró la auditoría del 06/10, y que
viven en `app.core.permisos` para que el criterio esté en un solo sitio:

- **`rol_id` y `empresa_id` son solo de admin**, ni siquiera con el valor que ya
  tenían: repartir roles y mover gente entre empresas no es cosa del gestor.
- **Un gestor no gestiona a un admin ni a otro gestor** de su empresa, tampoco
  su contraseña. Si no, compartir empresa con un admin bastaba para tomar su
  cuenta.
- **Nadie se cambia su propio rol ni su propio estado**, ni se da de baja a sí
  mismo (403). Un `usuario` editando su perfil sigue sin poder tocar
  `empresa_id`, `rol_id` ni `estado`.

El orden de comprobación es tenant primero (404) y rol después (403).

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
from app.core.permisos import ROL_USUARIO, UsuarioAutenticado
from app.core.security import hashear_password
from app.models import Empresa, Rol, Usuario
from app.schemas.common import Pagina
from app.schemas.usuario import EstadoUsuario, UsuarioCreate, UsuarioRead, UsuarioUpdate

router = APIRouter(prefix="/usuarios", tags=["usuarios"])

_NO_ENCONTRADO = HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")


async def _obtener_visible_o_404(
    db: DbDep, usuario_id: int, actual: UsuarioAutenticado
) -> tuple[Usuario, str]:
    """El usuario objetivo **y el código de su rol**, en una sola consulta.

    404 también cuando existe pero es de otra empresa: a un cliente no se le
    confirma qué ids están ocupados en otro tenant. El rol hace falta para
    decidir si quien pide puede gestionarlo, así que se trae de una vez en vez
    de con una segunda consulta.
    """
    fila = (
        await db.execute(
            select(Usuario, Rol.codigo).join(Rol, Rol.id == Usuario.rol_id).where(Usuario.id == usuario_id)
        )
    ).first()
    if fila is None or not actual.puede_ver_empresa(fila[0].empresa_id):
        raise _NO_ENCONTRADO
    usuario, rol_codigo = fila
    return usuario, rol_codigo


async def _exigir_email_libre(db: DbDep, email: str, excluir_id: int | None = None) -> None:
    consulta = select(Usuario.id).where(Usuario.email == email)
    if excluir_id is not None:
        consulta = consulta.where(Usuario.id != excluir_id)
    if (await db.execute(consulta)).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Ya existe un usuario con el email {email}.")


async def _exigir_referencias_validas(
    db: DbDep, empresa_id: int | None, rol_id: int | None
) -> Rol | None:
    """400 en vez de dejar que asyncpg devuelva un ForeignKeyViolationError:
    el cliente ha mandado datos mal, no es un fallo del servidor.

    Devuelve el `Rol` que ya ha tenido que consultar, para que quien llame
    pueda comprobar si puede asignarlo sin repetir la consulta.
    """
    if empresa_id is not None and await db.get(Empresa, empresa_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"No existe la empresa {empresa_id}.")
    if rol_id is None:
        return None
    rol = await db.get(Rol, rol_id)
    if rol is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"No existe el rol {rol_id}.")
    return rol


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
    rol = await _exigir_referencias_validas(db, datos.empresa_id, datos.rol_id)
    # Y solo admin reparte roles de gestión: si no, un gestor se crearía un
    # admin y entraría con él. `rol` no puede ser None (`rol_id` es obligatorio
    # en UsuarioCreate), pero si algún día dejara de serlo, el alta falla en
    # cerrado en vez de saltarse la comprobación del rol.
    if rol is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Hay que indicar el rol del usuario.")
    actual.exigir_rol_asignable(rol.codigo)
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
    usuario, _ = await _obtener_visible_o_404(db, usuario_id, actual)
    return usuario


@router.patch("/{usuario_id}", response_model=UsuarioRead, summary="Actualizar un usuario")
async def actualizar_usuario(
    usuario_id: int, datos: UsuarioUpdate, db: DbDep, actual: UsuarioActualDep
) -> Usuario:
    usuario, rol_objetivo = await _obtener_visible_o_404(db, usuario_id, actual)
    if actual.rol == ROL_USUARIO and usuario.id != actual.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Solo puedes editar tu propio perfil.")
    actual.exigir_gestion_de(usuario.id, rol_objetivo)

    cambios = datos.model_dump(exclude_unset=True)
    actual.exigir_campos_permitidos(cambios, objetivo_id=usuario.id)

    if "empresa_id" in cambios:
        actual.exigir_acceso_a_empresa(cambios["empresa_id"])
    rol_nuevo = await _exigir_referencias_validas(db, cambios.get("empresa_id"), cambios.get("rol_id"))
    if rol_nuevo is not None:
        actual.exigir_rol_asignable(rol_nuevo.codigo)
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
    usuario, rol_objetivo = await _obtener_visible_o_404(db, usuario_id, actual)
    # Evita que el único admin de una instalación se deje fuera.
    actual.exigir_no_es_uno_mismo(usuario.id)
    actual.exigir_gestion_de(usuario.id, rol_objetivo)
    usuario.estado = "inactivo"
    usuario.updated_by = actual.id
    await db.commit()
