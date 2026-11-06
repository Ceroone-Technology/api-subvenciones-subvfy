"""Address search for Mi empresa (CartoCiudad through the backend). Any role; see `app.services.direcciones`."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import UsuarioActualDep
from app.core.limite_intentos import LimiteIntentos
from app.services import direcciones as servicio
from app.services.direcciones import Direccion

router = APIRouter(prefix="/direcciones", tags=["direcciones"])

# 30 searches per minute per user (typing with a debounce stays far below). Per process, like every limiter here.
limite_busquedas = LimiteIntentos(maximo=30, ventana=60)


@router.get(
    "/buscar",
    response_model=list[Direccion],
    summary="Buscar una dirección (CartoCiudad)",
    responses={
        429: {"description": "Demasiadas búsquedas seguidas."},
        503: {"description": "El buscador de direcciones no está disponible: usar la entrada manual."},
    },
)
async def buscar_direcciones(
    actual: UsuarioActualDep,
    q: Annotated[str, Query(min_length=3, max_length=150, description="Texto de la dirección.")],
    limit: Annotated[int, Query(ge=1, le=10)] = 5,
) -> list[Direccion]:
    texto = " ".join(q.split())
    if len(texto) < 3:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="q necesita al menos 3 caracteres.")
    if limite_busquedas.bloqueado(actual.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Demasiadas búsquedas. Espera un momento.")
    limite_busquedas.registrar_fallo(actual.id)  # here every call counts, not only failures
    try:
        return await servicio.buscar(texto, limit)
    except servicio.DireccionesNoDisponible as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El buscador de direcciones no está disponible ahora. Puedes escribir la dirección a mano.",
        ) from exc
