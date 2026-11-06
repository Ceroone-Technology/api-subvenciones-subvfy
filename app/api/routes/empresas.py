"""CRUD de empresas (el tenant de la aplicación).

Permisos (ver `app.core.permisos` para el modelo completo):

| Acción              | admin        | gestor                  | usuario      |
|---------------------|--------------|-------------------------|--------------|
| Listar / consultar  | todas        | solo la suya            | solo la suya |
| Crear               | sí           | no                      | no           |
| Editar              | sí           | la suya, sin `estado`   | no           |
| Dar de baja         | menos la suya| no                      | no           |

Desde que **la baja de una empresa corta el acceso de toda su gente**
(AUD-009), su `estado` no es un campo cualquiera y tiene dos reglas:

- **Solo un admin lo cambia**, por presencia del campo: un gestor que lo
  envíe recibe 403 aunque sea para reactivar. Si pudiera, dejaría fuera a su
  empresa entera, él incluido.
- **Nadie da de baja la empresa en la que vive**, ni por `PATCH` ni por
  `DELETE`: se quedaría fuera en la petición siguiente y la única salida
  sería otro admin o el CLI. Lo que se rechaza es el valor que deja fuera,
  así que mandar `activa` sobre su propia empresa sí funciona.

Nota sobre el borrado: `DELETE /empresas/{id}` es una baja lógica
(`estado = "inactiva"`), no un DELETE físico. Una empresa es referenciada
por usuarios, alertas, análisis IA y por las columnas de auditoría
`created_by`/`updated_by` de medio esquema; borrarla de verdad o rompería
FKs o arrastraría datos históricos que el cliente necesita conservar.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select

from app.api.deps import AdminDep, DbDep, GestorDep, PaginacionDep, UsuarioActualDep
from app.core.permisos import ESTADO_EMPRESA_ACTIVA
from app.models import Empresa
from app.models.sector_actividad import CODIGO_ACTIVIDAD_OTRA
from app.schemas.common import Pagina
from app.schemas.empresa import (
    MENSAJE_OTRA_SIN_TEXTO,
    EmpresaCreate,
    EmpresaRead,
    EmpresaUpdate,
    EstadoEmpresa,
)
from app.services import empresas as servicio
from app.services import imagenes_perfil
from app.services.almacen_documentos import AlmacenDocumentos

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


def _resolver_actividad(empresa: Empresa, cambios: dict) -> None:
    """Deja `cambios` con una actividad coherente (la misma regla del CHECK de la tabla).

    El código efectivo es el nuevo o, si no viene, el actual. `OTRA` exige texto (el nuevo o el
    guardado); cualquier otro código descarta el texto libre."""
    codigo = cambios.get("actividad_codigo", empresa.actividad_codigo)
    if codigo == CODIGO_ACTIVIDAD_OTRA:
        if not cambios.get("actividad_otra", empresa.actividad_otra):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=MENSAJE_OTRA_SIN_TEXTO)
    else:
        cambios["actividad_otra"] = None


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
        items=await servicio.leer(db, list(result.scalars().all())),
        total=total or 0,
        page=paginacion.page,
        size=paginacion.size,
    )


@router.post("", response_model=EmpresaRead, status_code=status.HTTP_201_CREATED, summary="Crear empresa")
async def crear_empresa(datos: EmpresaCreate, db: DbDep, actual: AdminDep) -> EmpresaRead:
    await _exigir_nif_libre(db, datos.nif)
    campos = datos.model_dump(exclude={"intereses"})
    empresa = Empresa(**campos, created_by=actual.id, updated_by=actual.id)
    db.add(empresa)
    await db.flush()
    await servicio.reemplazar_intereses(db, empresa.id, datos.intereses or [], actual.id)
    await db.commit()
    await db.refresh(empresa)
    return (await servicio.leer(db, [empresa]))[0]


@router.get("/me", response_model=EmpresaRead, summary="Mi empresa, con la completitud del perfil")
async def mi_empresa(db: DbDep, actual: UsuarioActualDep) -> EmpresaRead:
    """Cualquier rol: devuelve la empresa del token. Editarla es `PATCH /empresas/{id}` (gestor o admin)."""
    empresa = await _obtener_o_404(db, actual.usuario.empresa_id)
    return (await servicio.leer(db, [empresa]))[0]


@router.get("/{empresa_id}", response_model=EmpresaRead, summary="Obtener una empresa")
async def obtener_empresa(empresa_id: int, db: DbDep, actual: UsuarioActualDep) -> EmpresaRead:
    actual.exigir_acceso_a_empresa(empresa_id)
    return (await servicio.leer(db, [await _obtener_o_404(db, empresa_id)]))[0]


@router.patch("/{empresa_id}", response_model=EmpresaRead, summary="Actualizar una empresa")
async def actualizar_empresa(
    empresa_id: int, datos: EmpresaUpdate, db: DbDep, actual: GestorDep
) -> EmpresaRead:
    actual.exigir_acceso_a_empresa(empresa_id)
    cambios = datos.model_dump(exclude_unset=True)
    actual.exigir_campos_de_empresa_permitidos(cambios)
    intereses = cambios.pop("intereses", None)
    if cambios.get("estado", ESTADO_EMPRESA_ACTIVA) != ESTADO_EMPRESA_ACTIVA:
        actual.exigir_no_es_su_propia_empresa(empresa_id)

    empresa = await _obtener_o_404(db, empresa_id)
    if "nif" in cambios:
        await _exigir_nif_libre(db, cambios["nif"], excluir_id=empresa_id)
    if "actividad_codigo" in cambios or "actividad_otra" in cambios:
        _resolver_actividad(empresa, cambios)
    for campo, valor in cambios.items():
        setattr(empresa, campo, valor)
    if intereses is not None:
        await servicio.reemplazar_intereses(db, empresa_id, intereses, actual.id)
    empresa.updated_by = actual.id
    await db.commit()
    await db.refresh(empresa)
    return (await servicio.leer(db, [empresa]))[0]


@router.put(
    "/{empresa_id}/logo",
    response_model=EmpresaRead,
    dependencies=[Depends(imagenes_perfil.exigir_tamano_razonable)],
    summary="Subir o cambiar el logo de la empresa",
    responses={
        400: {"description": "Imagen vacía, dañada o con demasiados píxeles."},
        413: {"description": "Más de 2 MB."},
        415: {"description": "No es JPEG, PNG ni WebP."},
    },
)
async def subir_logo(
    empresa_id: int,
    db: DbDep,
    actual: GestorDep,
    destino: Annotated[AlmacenDocumentos, Depends(imagenes_perfil.almacen)],
    archivo: Annotated[UploadFile, File(description="JPEG, PNG o WebP, máximo 2 MB.")],
) -> EmpresaRead:
    actual.exigir_acceso_a_empresa(empresa_id)
    empresa = await _obtener_o_404(db, empresa_id)
    empresa.logo_sha256 = await imagenes_perfil.guardar_subida(archivo, destino)
    empresa.updated_by = actual.id
    await db.commit()
    await db.refresh(empresa)
    return (await servicio.leer(db, [empresa]))[0]


@router.delete("/{empresa_id}/logo", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar el logo de la empresa")
async def quitar_logo(empresa_id: int, db: DbDep, actual: GestorDep) -> None:
    actual.exigir_acceso_a_empresa(empresa_id)
    empresa = await _obtener_o_404(db, empresa_id)
    if empresa.logo_sha256 is not None:
        empresa.logo_sha256 = None
        empresa.updated_by = actual.id
        await db.commit()


@router.get(
    "/{empresa_id}/logo",
    response_class=Response,
    summary="Logo de la empresa (autenticado)",
    responses={200: {"content": {"image/webp": {}}}, 304: {"description": "Sin cambios."}},
)
async def obtener_logo(
    empresa_id: int,
    request: Request,
    db: DbDep,
    actual: UsuarioActualDep,
    destino: Annotated[AlmacenDocumentos, Depends(imagenes_perfil.almacen)],
    v: Annotated[str | None, Query(description="Versión (de logo_url); solo afecta a la caché.")] = None,
) -> Response:
    actual.exigir_acceso_a_empresa(empresa_id)
    empresa = await _obtener_o_404(db, empresa_id)
    return await imagenes_perfil.servir(destino, empresa.logo_sha256, request, v)


@router.delete("/{empresa_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Dar de baja una empresa")
async def desactivar_empresa(empresa_id: int, db: DbDep, actual: AdminDep) -> None:
    actual.exigir_no_es_su_propia_empresa(empresa_id)
    empresa = await _obtener_o_404(db, empresa_id)
    empresa.estado = "inactiva"
    empresa.updated_by = actual.id
    await db.commit()
