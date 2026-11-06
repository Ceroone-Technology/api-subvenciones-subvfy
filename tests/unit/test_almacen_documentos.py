"""Document storage abstraction (PR10, task 10.4)."""

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.config import Settings
from app.services.almacen_documentos import (
    AlmacenLocal,
    ConfiguracionAlmacenInvalida,
    DocumentoDemasiadoGrande,
    clave_de,
    obtener_almacen,
)


async def _trozos(*partes: bytes, falla_tras: int | None = None) -> AsyncIterator[bytes]:
    for i, parte in enumerate(partes):
        if falla_tras is not None and i == falla_tras:
            raise RuntimeError("corte de red")
        yield parte


async def _leer(it: AsyncIterator[bytes]) -> bytes:
    return b"".join([c async for c in it])


def _archivos(raiz: Path) -> list[Path]:
    return [p for p in raiz.rglob("*") if p.is_file()]


async def test_guardar_devuelve_sha_y_tamano_y_abrir_lo_devuelve(tmp_path: Path) -> None:
    almacen = AlmacenLocal(tmp_path)
    datos = b"hola" * 1000
    sha = hashlib.sha256(datos).hexdigest()
    clave = clave_de(sha)
    assert clave == f"documentos/{sha[:2]}/{sha}"

    obtenido_sha, tamano = await almacen.guardar(_trozos(datos[:10], datos[10:]), max_bytes=10_000)
    assert (obtenido_sha, tamano) == (sha, len(datos))
    assert await almacen.existe(clave)
    assert await _leer(almacen.abrir(clave)) == datos
    assert almacen.url_temporal(clave) is None
    assert _archivos(tmp_path) == [tmp_path / "documentos" / sha[:2] / sha]


async def test_un_corte_a_mitad_no_deja_archivo_ni_temporal(tmp_path: Path) -> None:
    almacen = AlmacenLocal(tmp_path)
    with pytest.raises(RuntimeError):
        await almacen.guardar(_trozos(b"abc", b"def", falla_tras=1), max_bytes=100)
    assert _archivos(tmp_path) == []


async def test_superar_el_maximo_no_deja_archivo_ni_temporal(tmp_path: Path) -> None:
    almacen = AlmacenLocal(tmp_path)
    with pytest.raises(DocumentoDemasiadoGrande):
        await almacen.guardar(_trozos(b"a" * 6, b"b" * 6), max_bytes=10)
    assert _archivos(tmp_path) == []


async def test_existe_es_falso_si_no_esta(tmp_path: Path) -> None:
    assert not await AlmacenLocal(tmp_path).existe(clave_de("f" * 64))


@pytest.mark.parametrize("clave", ["../fuera", "documentos/../../x", "/abs", "documentos/zz/" + "g" * 64])
async def test_claves_invalidas_se_rechazan(tmp_path: Path, clave: str) -> None:
    with pytest.raises(ValueError):
        await AlmacenLocal(tmp_path).existe(clave)


def test_obtener_almacen_local_y_s3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    local = Settings(_env_file=None, documentos_backend="local", documentos_ruta=str(tmp_path))  # type: ignore[call-arg]
    assert isinstance(obtener_almacen(local), AlmacenLocal)
    s3 = Settings(_env_file=None, documentos_backend="s3")  # type: ignore[call-arg]
    with pytest.raises(ConfiguracionAlmacenInvalida):
        obtener_almacen(s3)
