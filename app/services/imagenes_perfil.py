"""Profile images (user avatar, company logo).

The upload is never trusted: the type comes from the magic bytes (not from the header or the extension), SVG and
anything but JPEG/PNG/WebP is refused, the pixel count is capped before decoding (decompression bombs), and the
image is re-encoded to WebP (max 256x256, EXIF/ICC dropped, orientation applied first). The stored bytes are
content-addressed through the same `AlmacenDocumentos` interface the grant documents use, so an S3 backend replaces
both at once; the DB keeps only the sha256 and the version in the public URL is derived from it.
"""

import asyncio
import io
import warnings
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from PIL import Image, ImageOps

from app.services.almacen_documentos import AlmacenDocumentos, clave_de, obtener_almacen

MAX_BYTES = 2 * 1024 * 1024
MAX_LADO = 256
MAX_PIXELES = 25_000_000
# Multipart framing adds a little to the file itself.
_MARGEN_MULTIPART = 64 * 1024
VERSION_LONGITUD = 16
MIME_SALIDA = "image/webp"

Image.MAX_IMAGE_PIXELS = MAX_PIXELES


class ImagenInvalida(ValueError):
    """The bytes claim to be an accepted image but cannot be processed safely (400)."""


class ImagenNoSoportada(ImagenInvalida):
    """Not JPEG, PNG or WebP (415)."""


def _formato_por_contenido(datos: bytes) -> str:
    if datos.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if len(datos) >= 12 and datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "WEBP"
    raise ImagenNoSoportada("Formato no admitido: solo JPEG, PNG o WebP.")


def procesar_imagen(datos: bytes) -> bytes:
    """Validates and re-encodes; returns the WebP bytes to store."""
    if not datos:
        raise ImagenInvalida("El archivo está vacío.")
    if len(datos) > MAX_BYTES:
        raise ImagenInvalida("La imagen supera 2 MB.")
    formato = _formato_por_contenido(datos)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(datos), formats=[formato]) as origen:
                ancho, alto = origen.size
                if ancho * alto > MAX_PIXELES:
                    raise ImagenInvalida("La imagen tiene demasiados píxeles.")
                origen.load()
                orientada = ImageOps.exif_transpose(origen)
                modo = "RGBA" if "A" in orientada.getbands() or "transparency" in orientada.info else "RGB"
                limpia = orientada.convert(modo)
    except ImagenInvalida:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImagenInvalida("La imagen tiene demasiados píxeles.") from None
    except Exception:  # Pillow raises many types for broken files (OSError, SyntaxError, ValueError...)
        raise ImagenInvalida("La imagen está dañada o no se puede leer.") from None
    limpia.info.clear()
    limpia.thumbnail((MAX_LADO, MAX_LADO))
    salida = io.BytesIO()
    limpia.save(salida, "WEBP", quality=85, method=4)
    return salida.getvalue()


def a_http(error: ImagenInvalida) -> HTTPException:
    codigo = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE if isinstance(error, ImagenNoSoportada) else 400
    return HTTPException(codigo, detail=str(error))


def exigir_tamano_razonable(request: Request) -> None:
    """Dependency: refuse an obviously oversized body before it is parsed."""
    declarado = request.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > MAX_BYTES + _MARGEN_MULTIPART:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="La imagen supera 2 MB.")


def almacen() -> AlmacenDocumentos:
    """FastAPI dependency (overridable in tests)."""
    return obtener_almacen()


async def _una_pieza(datos: bytes) -> AsyncIterator[bytes]:
    yield datos


async def guardar_subida(archivo: UploadFile, destino: AlmacenDocumentos) -> str:
    """Reads (capped), processes in a thread, stores; returns the sha256 of the stored WebP."""
    datos = await archivo.read(MAX_BYTES + 1)
    if len(datos) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="La imagen supera 2 MB.")
    try:
        webp = await asyncio.to_thread(procesar_imagen, datos)
    except ImagenInvalida as error:
        raise a_http(error) from None
    sha, _ = await destino.guardar(_una_pieza(webp), MAX_BYTES)
    return sha


def url_versionada(ruta: str, sha: str | None) -> str | None:
    return None if sha is None else f"{ruta}?v={sha[:VERSION_LONGITUD]}"


async def servir(destino: AlmacenDocumentos, sha: str | None, request: Request, version: str | None) -> Response:
    """Authenticated GET body: ETag, safe type, nosniff; long cache only when the URL carries the current version."""
    if sha is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Imagen no encontrada.")
    etag = f'"{sha}"'
    cache = (
        "private, max-age=31536000, immutable" if version == sha[:VERSION_LONGITUD] else "private, no-cache"
    )
    cabeceras = {"X-Content-Type-Options": "nosniff", "Cache-Control": cache, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=cabeceras)
    clave = clave_de(sha)
    if not await destino.existe(clave):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Imagen no encontrada.")
    return StreamingResponse(destino.abrir(clave), media_type=MIME_SALIDA, headers=cabeceras)
