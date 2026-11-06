"""Servir documentos de la BDNS desde nuestro almacén (`bdns-local-sync`, PR11, D14-D16).

Flujo: el `id` tiene que estar en el detalle guardado de la convocatoria; la primera vez
se descarga de la BDNS con el cliente de sincronización (throttle compartido), se guarda
por hash y se registran los metadatos; después se sirve del almacén. Cada servicio exitoso
deja un evento de acceso con solo ids (sin email, IP ni contenido). Los logs, igual.
"""

import logging
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Convocatoria, ConvocatoriaDocumento, DocumentoAcceso
from app.services.almacen_documentos import (
    AlmacenDocumentos,
    DocumentoDemasiadoGrande,
    clave_de,
    obtener_almacen,
)
from app.services.bdns_cliente import BdnsNoDisponible
from app.services.bdns_sync_cliente import CircuitoAbierto, ClienteBdnsSync, nuevo_cubo

logger = logging.getLogger(__name__)

# Cubo compartido por todos los requests del proceso: varias descargas simultáneas no suman peticiones/s.
_CUBO = nuevo_cubo(settings.bdns_sync_rps)


class DocumentoNoEncontrado(Exception):
    pass


class BdnsDocumentoNoDisponible(Exception):
    pass


@dataclass(frozen=True)
class DocumentoListo:
    clave: str
    mime: str
    nombre: str
    tamano: int
    origen: str


def max_bytes() -> int:
    return settings.documentos_max_mb * 1024 * 1024


def almacen() -> AlmacenDocumentos:
    """Dependencia de FastAPI (sustituible en los tests)."""
    return obtener_almacen()


async def cliente_bdns() -> AsyncGenerator[ClienteBdnsSync, None]:
    """Dependencia de FastAPI: un cliente por petición, con el cubo de tokens compartido.

    El breaker es **por petición**, a propósito: la cuenta de fallos seguidos nace a cero con cada cliente, así que
    nunca se abre entre peticiones distintas (solo corta los reintentos de una misma descarga). Lo que sí se
    comparte, y limita la presión sobre la BDNS, es el cubo de tokens. Un breaker global exigiría estado de proceso,
    que en Lambda no se conserva (Hito 7).
    """
    async with ClienteBdnsSync(
        cubo=_CUBO,
        vpd=settings.bdns_vpd,
        umbral_breaker=settings.bdns_sync_breaker,
    ) as cliente:
        yield cliente


_NO_SEGURO = re.compile(r'[\x00-\x1f\x7f"\\/;:*?<>|]+')


def nombre_seguro(*candidatos: str | None, id_documento: int) -> str:
    for candidato in candidatos:
        if candidato and (limpio := _NO_SEGURO.sub("_", candidato).strip(" ._")[:150]):
            return limpio
    return f"documento-{id_documento}"


_MIME_VALIDO = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,99}/[a-z0-9][a-z0-9!#$&^_.+-]{0,99}$")
# Tipos que un navegador ejecuta o interpreta como página: nunca se sirven como lo que dice la BDNS.
_MIME_ACTIVO = {
    "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
    "text/javascript", "application/javascript",
}  # fmt: skip


def mime_seguro(mime: str | None) -> str:
    """El `Content-Type` llega de la BDNS: se sirve solo si tiene forma de tipo MIME y no es activo."""
    limpio = (mime or "").strip().lower()
    if not _MIME_VALIDO.match(limpio) or limpio in _MIME_ACTIVO:
        return "application/octet-stream"
    return limpio


def _ids_de_documentos(detalle: dict[str, Any] | None) -> dict[int, str | None]:
    docs = (detalle or {}).get("documentos") or []
    return {d["id"]: d.get("nombre") for d in docs if isinstance(d, dict) and isinstance(d.get("id"), int)}


async def servir(
    db: AsyncSession,
    *,
    usuario_id: int,
    codigo_bdns: str,
    id_documento: int,
    almacen_documentos: AlmacenDocumentos,
    cliente: ClienteBdnsSync,
) -> DocumentoListo:
    fila = (
        await db.execute(select(Convocatoria.id, Convocatoria.detalle).where(Convocatoria.codigo_bdns == codigo_bdns))
    ).one_or_none()
    if fila is None:
        raise DocumentoNoEncontrado
    conv_id, detalle = fila
    documentos = _ids_de_documentos(detalle)
    if id_documento not in documentos:
        raise DocumentoNoEncontrado
    nombre_ficha = documentos[id_documento]

    doc = (
        await db.execute(
            select(ConvocatoriaDocumento).where(
                ConvocatoriaDocumento.convocatoria_id == conv_id,
                ConvocatoriaDocumento.bdns_documento_id == id_documento,
            )
        )
    ).scalar_one_or_none()
    if doc is not None and doc.sha256 and await almacen_documentos.existe(clave_de(doc.sha256)):
        resultado = DocumentoListo(
            clave_de(doc.sha256),
            mime_seguro(doc.mime),
            nombre_seguro(doc.nombre_archivo, nombre_ficha, id_documento=id_documento),
            doc.tamano or 0,
            "cache",
        )
        doc_id = doc.id
    else:
        await db.rollback()  # no mantener una conexión abierta durante una descarga de hasta decenas de segundos
        try:
            async with cliente.descargar_documento(id_documento, max_bytes=max_bytes()) as descarga:
                sha, tamano = await almacen_documentos.guardar(descarga.contenido, max_bytes())
                mime, nombre_bdns = mime_seguro(descarga.mime), descarga.nombre_archivo
        except (BdnsNoDisponible, CircuitoAbierto) as exc:
            logger.warning("Doc %s (conv. %s): BDNS no disponible (%s).", id_documento, conv_id, type(exc).__name__)
            raise BdnsDocumentoNoDisponible from exc
        except DocumentoDemasiadoGrande:
            logger.warning("Documento %s de la convocatoria %s supera el máximo.", id_documento, conv_id)
            raise
        nombre = nombre_seguro(nombre_ficha, nombre_bdns, id_documento=id_documento)
        ahora = datetime.now(UTC)
        valores = {
            "sha256": sha, "tamano": tamano, "mime": mime[:150], "nombre_archivo": nombre, "fetched_at": ahora,
            "updated_by": usuario_id,
        }  # fmt: skip
        doc_id_nuevo = await db.scalar(
            insert(ConvocatoriaDocumento)
            .values(convocatoria_id=conv_id, bdns_documento_id=id_documento, created_by=usuario_id, **valores)
            .on_conflict_do_update(constraint="uq_convocatoria_documento_bdns", set_=valores)
            .returning(ConvocatoriaDocumento.id)
        )
        assert doc_id_nuevo is not None
        doc_id = doc_id_nuevo
        resultado = DocumentoListo(clave_de(sha), mime, nombre, tamano, "bdns")

    db.add(
        DocumentoAcceso(
            documento_id=doc_id, usuario_id=usuario_id, origen=resultado.origen,
            created_by=usuario_id, updated_by=usuario_id,
        )  # fmt: skip
    )
    await db.commit()
    logger.info("Documento %s servido (origen=%s, usuario=%s).", doc_id, resultado.origen, usuario_id)
    return resultado


async def metricas(
    db: AsyncSession, *, desde: date | None, hasta: date | None, offset: int, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Accesos por documento (total, usuarios distintos, último), del más al menos descargado."""
    donde = []
    if desde is not None:
        donde.append(DocumentoAcceso.accedido_at >= datetime.combine(desde, datetime.min.time(), tzinfo=UTC))
    if hasta is not None:
        donde.append(
            DocumentoAcceso.accedido_at < datetime.combine(hasta + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        )
    agregado = (
        select(
            DocumentoAcceso.documento_id.label("documento_id"),
            func.count().label("accesos"),
            func.count(func.distinct(DocumentoAcceso.usuario_id)).label("usuarios_distintos"),
            func.max(DocumentoAcceso.accedido_at).label("ultimo_acceso"),
        )
        .where(*donde)
        .group_by(DocumentoAcceso.documento_id)
        .subquery()
    )
    total = await db.scalar(select(func.count()).select_from(agregado)) or 0
    filas = await db.execute(
        select(
            Convocatoria.codigo_bdns.label("convocatoria_codigo"),
            ConvocatoriaDocumento.bdns_documento_id,
            ConvocatoriaDocumento.nombre_archivo.label("nombre"),
            agregado.c.accesos,
            agregado.c.usuarios_distintos,
            agregado.c.ultimo_acceso,
        )
        .join(ConvocatoriaDocumento, ConvocatoriaDocumento.id == agregado.c.documento_id)
        .join(Convocatoria, Convocatoria.id == ConvocatoriaDocumento.convocatoria_id)
        .order_by(agregado.c.accesos.desc(), ConvocatoriaDocumento.id)
        .offset(offset)
        .limit(limit)
    )
    return total, [dict(f._mapping) for f in filas]
