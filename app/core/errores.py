"""Manejador de errores no controlados.

Starlette coloca `ServerErrorMiddleware` por fuera de `CORSMiddleware`: una excepción sin
capturar sale como 500 sin cabeceras CORS y el navegador lo ve como un fallo de red. Este
manejador devuelve un 500 JSON genérico y añade las cabeceras CORS según los orígenes
configurados. Nunca expone la excepción al cliente y la registra sin datos personales.
"""

import logging
import traceback
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.permisos import CuentaNoOperativa

logger = logging.getLogger("app.errores")

MENSAJE_ERROR_INTERNO = "Ha ocurrido un error inesperado. Inténtalo de nuevo más tarde."


def crear_manejador_errores(
    origenes_permitidos: list[str],
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    permitidos = set(origenes_permitidos)

    async def manejar(request: Request, exc: Exception) -> JSONResponse:
        # Método, ruta, tipo de excepción y pila. Sin el mensaje de la excepción (los de la BD
        # llevan parámetros), ni query string, cabeceras o cuerpo: pueden contener datos personales.
        logger.error(
            "Error no controlado en %s %s: %s\n%s",
            request.method,
            request.url.path,
            type(exc).__name__,
            "".join(traceback.format_tb(exc.__traceback__)),
        )
        headers = {"Vary": "Origin"}
        origen = request.headers.get("origin")
        if origen is not None and ("*" in permitidos or origen in permitidos):
            headers["Access-Control-Allow-Origin"] = origen
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        return JSONResponse({"detail": MENSAJE_ERROR_INTERNO}, status_code=500, headers=headers)

    return manejar


async def cuenta_no_operativa_handler(request: Request, exc: Exception) -> JSONResponse:
    """403 de cuenta inactiva: `{"detail": <texto>, "codigo": <código estable>}`."""
    assert isinstance(exc, CuentaNoOperativa)
    return JSONResponse({"detail": exc.detail, "codigo": exc.codigo}, status_code=exc.status_code)
