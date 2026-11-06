"""Admin client panel (admin only): users across companies and the file of one client company.

Every admin read of a client company is logged in `admin_lectura_cliente` (who, which company, when). Responses never
carry passwords, hashes or tokens. Management requests (`/admin/solicitudes`) and the password reset belong to N14 and
N13b and are added with them.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import AdminDep, DbDep, PaginacionDep
from app.models import (
    AdminLecturaCliente,
    Alerta,
    Convocatoria,
    Empresa,
    Favorito,
    Rol,
    Seguimiento,
    Usuario,
)
from app.models.seguimiento import ESTADOS_SEGUIMIENTO
from app.schemas.admin_clientes import (
    AdminEmpresaDetalle,
    AdminUsuarioRead,
    AdminUsuarioUpdate,
    AlertasResumen,
    FavoritoResumen,
    RolCodigo,
    SeguimientoPorEstado,
    UsuarioResumen,
)
from app.schemas.common import Pagina
from app.schemas.usuario import EstadoUsuario
from app.services import empresas as servicio_empresas

router = APIRouter(prefix="/admin", tags=["admin-clientes"])

FAVORITOS_RECIENTES = 20
_USUARIO_NO_ENCONTRADO = HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")


async def _registrar_lectura(db: DbDep, admin_id: int, empresa_id: int, accion: str) -> None:
    db.add(
        AdminLecturaCliente(
            admin_usuario_id=admin_id, empresa_id=empresa_id, accion=accion, created_by=admin_id, updated_by=admin_id
        )
    )


@router.get("/usuarios", response_model=Pagina[AdminUsuarioRead], summary="Usuarios de todas las empresas")
async def listar_usuarios(
    db: DbDep,
    actual: AdminDep,
    paginacion: PaginacionDep,
    q: Annotated[str | None, Query(max_length=100, description="Nombre, apellidos o email.")] = None,
    empresa_id: int | None = None,
    rol: RolCodigo | None = None,
    estado: EstadoUsuario | None = None,
    acceso_desde: datetime | None = None,
    acceso_hasta: datetime | None = None,
) -> Pagina[AdminUsuarioRead]:
    donde = []
    if q and q.strip():
        patron = f"%{q.strip().replace('%', '').replace('_', '')}%"
        donde.append(
            or_(Usuario.nombre.ilike(patron), Usuario.apellidos.ilike(patron), Usuario.email.ilike(patron))
        )
    if empresa_id is not None:
        donde.append(Usuario.empresa_id == empresa_id)
    if rol is not None:
        donde.append(Rol.codigo == rol)
    if estado is not None:
        donde.append(Usuario.estado == estado)
    if acceso_desde is not None:
        donde.append(Usuario.ultimo_acceso_at >= acceso_desde)
    if acceso_hasta is not None:
        donde.append(Usuario.ultimo_acceso_at <= acceso_hasta)
    base = (
        select(Usuario, Rol.codigo, Empresa.razon_social)
        .join(Rol, Rol.id == Usuario.rol_id)
        .join(Empresa, Empresa.id == Usuario.empresa_id)
        .where(*donde)
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    filas = await db.execute(base.order_by(Usuario.id.desc()).offset(paginacion.offset).limit(paginacion.size))
    items = [
        AdminUsuarioRead(
            id=u.id, nombre=u.nombre, apellidos=u.apellidos, email=u.email, empresa_id=u.empresa_id,
            empresa=razon, rol=codigo, estado=u.estado, ultimo_acceso_at=u.ultimo_acceso_at, created_at=u.created_at,
        )  # fmt: skip
        for u, codigo, razon in filas
    ]
    if empresa_id is not None and await db.get(Empresa, empresa_id) is not None:
        await _registrar_lectura(db, actual.id, empresa_id, "usuarios")
        await db.commit()
    return Pagina(items=items, total=total, page=paginacion.page, size=paginacion.size)


@router.patch(
    "/usuarios/{usuario_id}",
    response_model=AdminUsuarioRead,
    summary="Cambiar el rol o el estado de un usuario",
    responses={
        403: {"description": "No puedes cambiarte tu propio rol o estado."},
        404: {"description": "Usuario desconocido."},
        409: {"description": "Dejaría la plataforma sin administradores activos."},
    },
)
async def actualizar_usuario(
    usuario_id: int, datos: AdminUsuarioUpdate, db: DbDep, actual: AdminDep
) -> AdminUsuarioRead:
    cambios = datos.model_dump(exclude_unset=True)
    if not cambios:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Indica el rol o el estado.")
    usuario = await db.get(Usuario, usuario_id)
    if usuario is None:
        raise _USUARIO_NO_ENCONTRADO
    if usuario.id == actual.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="No puedes cambiar tu propio rol ni tu estado.")
    codigo_actual = await db.scalar(select(Rol.codigo).where(Rol.id == usuario.rol_id))
    nuevo_codigo = cambios.get("rol", codigo_actual)
    nuevo_estado = cambios.get("estado", usuario.estado)
    deja_de_ser_admin_activo = codigo_actual == "admin" and (nuevo_codigo != "admin" or nuevo_estado != "activo")
    if deja_de_ser_admin_activo:
        otros = await db.scalar(
            select(func.count())
            .select_from(Usuario)
            .join(Rol, Rol.id == Usuario.rol_id)
            .where(Rol.codigo == "admin", Usuario.estado == "activo", Usuario.id != usuario.id)
        )
        if not otros:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="Debe quedar al menos un administrador activo.")
    if "rol" in cambios:
        rol_id = await db.scalar(select(Rol.id).where(Rol.codigo == nuevo_codigo))
        if rol_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Rol desconocido.")
        usuario.rol_id = rol_id
    if "estado" in cambios:
        usuario.estado = nuevo_estado
    usuario.updated_by = actual.id
    await db.commit()
    razon = await db.scalar(select(Empresa.razon_social).where(Empresa.id == usuario.empresa_id))
    await db.refresh(usuario)
    return AdminUsuarioRead(
        id=usuario.id, nombre=usuario.nombre, apellidos=usuario.apellidos, email=usuario.email,
        empresa_id=usuario.empresa_id, empresa=razon or "", rol=str(nuevo_codigo), estado=usuario.estado,
        ultimo_acceso_at=usuario.ultimo_acceso_at, created_at=usuario.created_at,
    )  # fmt: skip


@router.get(
    "/empresas/{empresa_id}",
    response_model=AdminEmpresaDetalle,
    summary="Ficha de una empresa cliente (la lectura queda registrada)",
    responses={404: {"description": "Empresa desconocida."}},
)
async def ficha_empresa(empresa_id: int, db: DbDep, actual: AdminDep) -> AdminEmpresaDetalle:
    empresa = await db.get(Empresa, empresa_id)
    if empresa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada.")
    (perfil,) = await servicio_empresas.leer(db, [empresa])

    usuarios = [
        UsuarioResumen(
            id=u.id, nombre=u.nombre, apellidos=u.apellidos, email=u.email, rol=codigo, estado=u.estado,
            ultimo_acceso_at=u.ultimo_acceso_at,
        )  # fmt: skip
        for u, codigo in (
            await db.execute(
                select(Usuario, Rol.codigo)
                .join(Rol, Rol.id == Usuario.rol_id)
                .where(Usuario.empresa_id == empresa_id)
                .order_by(Usuario.id)
            )
        ).all()
    ]
    ids = [u.id for u in usuarios]
    consulta_estados = (
        select(Seguimiento.estado, func.count())
        .where(Seguimiento.empresa_id == empresa_id)
        .group_by(Seguimiento.estado)
    )
    por_estado: dict[str, int] = {e: n for e, n in (await db.execute(consulta_estados)).all()}
    seguimiento = [SeguimientoPorEstado(estado=e, total=por_estado.get(e, 0)) for e in ESTADOS_SEGUIMIENTO]

    favoritos_total = (
        await db.scalar(select(func.count()).select_from(Favorito).where(Favorito.usuario_id.in_(ids))) if ids else 0
    ) or 0
    favoritos = []
    alertas = AlertasResumen(total=0, activas=0)
    if ids:
        favoritos = [
            FavoritoResumen(usuario_id=uid, codigo_bdns=codigo, titulo=titulo, created_at=creado)
            for uid, codigo, titulo, creado in (
                await db.execute(
                    select(Favorito.usuario_id, Convocatoria.codigo_bdns, Convocatoria.titulo, Favorito.created_at)
                    .join(Convocatoria, Convocatoria.id == Favorito.convocatoria_id)
                    .where(Favorito.usuario_id.in_(ids))
                    .order_by(Favorito.created_at.desc(), Favorito.id.desc())
                    .limit(FAVORITOS_RECIENTES)
                )
            ).all()
        ]
        total_alertas, activas = (
            await db.execute(
                select(func.count(), func.count().filter(Alerta.activa.is_(True))).where(Alerta.usuario_id.in_(ids))
            )
        ).one()
        alertas = AlertasResumen(total=total_alertas, activas=activas)

    await _registrar_lectura(db, actual.id, empresa_id, "ficha")
    await db.commit()
    return AdminEmpresaDetalle(
        empresa=perfil, usuarios=usuarios, seguimiento=seguimiento, favoritos_total=favoritos_total,
        favoritos=favoritos, alertas=alertas, solicitudes_recientes=[],
    )  # fmt: skip
