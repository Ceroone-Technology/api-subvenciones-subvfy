"""CRUD de empresas (el tenant de la aplicación).

Permisos (ver `app.core.permisos` para el modelo completo):

| Acción              | admin | gestor        | usuario       |
|---------------------|-------|---------------|---------------|
| Listar / consultar  | todas | solo la suya  | solo la suya  |
| Crear               | sí    | no            | no            |
| Editar              | sí    | solo la suya  | no            |
| Dar de baja         | sí    | no            | no            |

Nota sobre el borrado: `DELETE /empresas/{id}` es una baja lógica
(`estado = "inactiva"`), no un DELETE físico. Una empresa es referenciada
por usuarios, alertas, análisis IA y por las columnas de auditoría
`created_by`/`updated_by` de medio esquema; borrarla de verdad o rompería
FKs o arrastraría datos históricos que el cliente necesita conservar.
"""

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import AdminDep, DbDep, GestorDep, PaginacionDep, UsuarioActualDep
from app.models import Empresa
from app.schemas.common import Pagina
from app.schemas.empresa import EmpresaCreate, EmpresaRead, EmpresaUpdate, EstadoEmpresa

router = APIRouter(prefix="/empresas", tags=["empresas"])


async def _obtener_o_404(db: DbDep, empresa_id: int) -> Empresa:
    empresa = await db.get(Empresa, empresa_id)
    if empresa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada.")
    return empresa


async def _exigir_nif_libre(db: DbDep, nif: str, excluir_id: int | None = None) -> None:
    """El NIF es UNIQUE: se comprueba antes de insertar para devolver un 409
    con mensaje útil en vez de dejar que reviente el IntegrityError."""
    consulta = select(Empresa.id).where(Empresa.nif == nif)
    if excluir_id is not None:
        consulta = consulta.where(Empresa.id != excluir_id)
    if (await db.execute(consulta)).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Ya existe una empresa con el NIF {nif}.")


@router.get("", response_model=Pagina[EmpresaRead], summary="Listar empresas")
async def listar_empresas(
    db: DbDep,
    paginacion: PaginacionDep,
    actual: UsuarioActualDep,
    q: str | None = Query(default=None, description="Busca en razón social y NIF."),
    estado: EstadoEmpresa | None = Query(default=None),
) -> Pagina[EmpresaRead]:
    filtros = []
    # Aislamiento entre clientes: quien no es admin solo se ve a sí mismo,
    # y no porque lo pida, sino porque el filtro se impone aquí.
    if actual.filtro_empresa is not None:
        filtros.append(Empresa.id == actual.filtro_empresa)
    if q:
        patron = f"%{q.strip()}%"
        filtros.append(or_(Empresa.razon_social.ilike(patron), Empresa.nif.ilike(patron)))
    if estado:
        filtros.append(Empresa.estado == estado)

    total = await db.scalar(select(func.count()).select_from(Empresa).where(*filtros))
    result = await db.execute(
        select(Empresa)
        .where(*filtros)
        .order_by(Empresa.razon_social)
        .offset(paginacion.offset)
        .limit(paginacion.size)
    )
    return Pagina[EmpresaRead](
        items=[EmpresaRead.model_validate(e) for e in result.scalars().all()],
        total=total or 0,
        page=paginacion.page,
        size=paginacion.size,
    )


@router.post("", response_model=EmpresaRead, status_code=status.HTTP_201_CREATED, summary="Crear empresa")
async def crear_empresa(datos: EmpresaCreate, db: DbDep, actual: AdminDep) -> Empresa:
    await _exigir_nif_libre(db, datos.nif)
    empresa = Empresa(**datos.model_dump(), created_by=actual.id, updated_by=actual.id)
    db.add(empresa)
    await db.commit()
    await db.refresh(empresa)
    return empresa


@router.get("/{empresa_id}", response_model=EmpresaRead, summary="Obtener una empresa")
async def obtener_empresa(empresa_id: int, db: DbDep, actual: UsuarioActualDep) -> Empresa:
    actual.exigir_acceso_a_empresa(empresa_id)
    return await _obtener_o_404(db, empresa_id)


@router.patch("/{empresa_id}", response_model=EmpresaRead, summary="Actualizar una empresa")
async def actualizar_empresa(
    empresa_id: int, datos: EmpresaUpdate, db: DbDep, actual: GestorDep
) -> Empresa:
    actual.exigir_acceso_a_empresa(empresa_id)
    empresa = await _obtener_o_404(db, empresa_id)
    cambios = datos.model_dump(exclude_unset=True)
    if "nif" in cambios:
        await _exigir_nif_libre(db, cambios["nif"], excluir_id=empresa_id)
    for campo, valor in cambios.items():
        setattr(empresa, campo, valor)
    empresa.updated_by = actual.id
    await db.commit()
    await db.refresh(empresa)
    return empresa


@router.delete("/{empresa_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Dar de baja una empresa")
async def desactivar_empresa(empresa_id: int, db: DbDep, actual: AdminDep) -> None:
    empresa = await _obtener_o_404(db, empresa_id)
    empresa.estado = "inactiva"
    empresa.updated_by = actual.id
    await db.commit()
