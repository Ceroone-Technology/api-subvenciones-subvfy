"""Almacén de documentos de la BDNS (`bdns-local-sync`, PR10, D14-D15).

Direccionado por contenido: la clave es `documentos/<sha256[0:2]>/<sha256>` y nunca
sale de lo que escribe el usuario, así que no hay recorrido de rutas. `AlmacenLocal`
escribe en un temporal y hace `os.replace`; el interfaz (`AlmacenDocumentos`) deja
sitio a una implementación S3 en el Hito 7 (`url_temporal` devolverá una URL firmada
de nuestro bucket; en local es `None` y la ruta sirve el contenido).
"""

import asyncio
import hashlib
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from app.config import Settings, settings

CHUNK = 64 * 1024
_CLAVE = re.compile(r"^documentos/([0-9a-f]{2})/([0-9a-f]{64})$")
# Un temporal de esta edad es resto de un proceso muerto (un fallo normal lo borra en `guardar`).
EDAD_TEMPORAL_HUERFANO_SEGUNDOS = 3600


def _barrer_temporales(tmp_dir: Path) -> None:
    """Borra los temporales viejos que dejó un proceso interrumpido (un `kill` salta el `except` de `guardar`)."""
    limite = time.time() - EDAD_TEMPORAL_HUERFANO_SEGUNDOS
    for fichero in tmp_dir.iterdir():
        try:
            if fichero.is_file() and fichero.stat().st_mtime < limite:
                fichero.unlink()
        except OSError:  # otro proceso lo borró o lo está usando: no es motivo para fallar el guardado
            continue


class DocumentoDemasiadoGrande(Exception):
    """El contenido supera el máximo permitido; no se guarda nada."""


class ConfiguracionAlmacenInvalida(RuntimeError):
    """El backend de almacenamiento configurado no está disponible."""


def clave_de(sha256: str) -> str:
    return f"documentos/{sha256[:2]}/{sha256}"


class AlmacenDocumentos(Protocol):
    async def guardar(self, origen: AsyncIterator[bytes], max_bytes: int) -> tuple[str, int]: ...
    def abrir(self, clave: str) -> AsyncIterator[bytes]: ...
    async def existe(self, clave: str) -> bool: ...
    def url_temporal(self, clave: str) -> str | None: ...


class AlmacenLocal:
    def __init__(self, raiz: Path | str) -> None:
        self._raiz = Path(raiz)

    def _ruta(self, clave: str) -> Path:
        m = _CLAVE.match(clave)
        if m is None or m.group(2)[:2] != m.group(1):
            raise ValueError("Clave de documento inválida.")
        return self._raiz / clave

    async def guardar(self, origen: AsyncIterator[bytes], max_bytes: int) -> tuple[str, int]:
        """Escribe el flujo (con tope) y devuelve `(sha256, tamano)`. Ante cualquier fallo no deja nada."""
        tmp_dir = self._raiz / "tmp"
        await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(_barrer_temporales, tmp_dir)
        tmp = tmp_dir / uuid.uuid4().hex
        sha = hashlib.sha256()
        tamano = 0
        try:
            with tmp.open("wb") as fichero:
                async for trozo in origen:
                    tamano += len(trozo)
                    if tamano > max_bytes:
                        raise DocumentoDemasiadoGrande(f"El documento supera {max_bytes} bytes.")
                    sha.update(trozo)
                    await asyncio.to_thread(fichero.write, trozo)
            digest = sha.hexdigest()
            destino = self._ruta(clave_de(digest))
            await asyncio.to_thread(destino.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(os.replace, tmp, destino)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return digest, tamano

    async def abrir(self, clave: str) -> AsyncIterator[bytes]:
        ruta = self._ruta(clave)
        with ruta.open("rb") as fichero:
            while trozo := await asyncio.to_thread(fichero.read, CHUNK):
                yield trozo

    async def existe(self, clave: str) -> bool:
        return await asyncio.to_thread(self._ruta(clave).is_file)

    def url_temporal(self, clave: str) -> str | None:
        return None


def obtener_almacen(config: Settings | None = None) -> AlmacenDocumentos:
    config = config or settings
    if config.documentos_backend == "local":
        return AlmacenLocal(config.documentos_ruta)
    raise ConfiguracionAlmacenInvalida(
        f"DOCUMENTOS_BACKEND={config.documentos_backend!r} no está disponible "
        "(válido hoy: local; S3 llega en el Hito 7)."
    )
