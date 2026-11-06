"""Documents served from our own storage + admin metrics (PR11, task 11.3).

BDNS is always mocked (`httpx.MockTransport`); storage is a `tmp_path` local store.
"""

import hashlib
import logging
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.main import app
from app.models import Convocatoria, ConvocatoriaDocumento, DocumentoAcceso
from app.services import documentos
from app.services.almacen_documentos import AlmacenLocal
from app.services.bdns_sync_cliente import ClienteBdnsSync
from tests.conftest import Sesion, crear_convocatoria_en_bd, crear_sesion_en_bd

CONTENIDO = b"%PDF-1.4 contenido secreto del documento"
SHA = hashlib.sha256(CONTENIDO).hexdigest()
ID_DOC = 4242


class Bdns:
    """BDNS falsa: cuenta llamadas y responde lo que se le diga."""

    def __init__(self) -> None:
        self.llamadas = 0
        self.respuesta: Callable[[], httpx.Response] = lambda: httpx.Response(
            200,
            content=CONTENIDO,
            headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="bases.pdf"'},
        )

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.llamadas += 1
        return self.respuesta()


@pytest.fixture
def bdns() -> Bdns:
    return Bdns()


@pytest.fixture
def almacen_dir(tmp_path: Path) -> Path:
    return tmp_path / "almacen"


@pytest_asyncio.fixture(autouse=True)
async def entorno(bdns: Bdns, almacen_dir: Path) -> AsyncGenerator[None, None]:
    reloj = {"t": 1e9}

    async def sin_esperas(s: float) -> None:
        reloj["t"] += s
        return None

    async def cliente() -> AsyncGenerator[ClienteBdnsSync, None]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(bdns), base_url="https://bdns.example/api")
        yield ClienteBdnsSync(http, dormir=sin_esperas, reloj=lambda: reloj["t"])
        await http.aclose()

    app.dependency_overrides[documentos.cliente_bdns] = cliente
    app.dependency_overrides[documentos.almacen] = lambda: AlmacenLocal(almacen_dir)
    yield
    app.dependency_overrides.pop(documentos.cliente_bdns, None)
    app.dependency_overrides.pop(documentos.almacen, None)


async def convocatoria_con_documento(id_doc: int = ID_DOC) -> str:
    conv = await crear_convocatoria_en_bd(
        "Ayudas con documento", detalle={"documentos": [{"id": id_doc, "nombre": "bases.pdf"}]}
    )
    return conv.codigo_bdns


def url(codigo: str, id_doc: int = ID_DOC) -> str:
    return f"/convocatorias/{codigo}/documentos/{id_doc}"


def archivos(raiz: Path) -> list[Path]:
    return [p for p in raiz.rglob("*") if p.is_file()] if raiz.exists() else []


async def filas_documento(codigo: str) -> list[ConvocatoriaDocumento]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(ConvocatoriaDocumento).where(
                        ConvocatoriaDocumento.convocatoria_id.in_(
                            select(Convocatoria.id).where(Convocatoria.codigo_bdns == codigo)
                        )
                    )
                )
            )
            .scalars()
            .all()
        )


async def eventos(usuario_ids: list[int]) -> list[tuple[int, str]]:
    async with AsyncSessionLocal() as db:
        filas = await db.execute(
            select(DocumentoAcceso.usuario_id, DocumentoAcceso.origen)
            .where(DocumentoAcceso.usuario_id.in_(usuario_ids))
            .order_by(DocumentoAcceso.id)
        )
        return [(u, o) for u, o in filas]


async def cliente_de(sesion: Sesion) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=sesion.headers)


# --- Serve flow --------------------------------------------------------------


async def test_primera_descarga_baja_de_bdns_guarda_y_sirve(
    client_usuario: AsyncClient, bdns: Bdns, almacen_dir: Path
) -> None:
    codigo = await convocatoria_con_documento()
    r = await client_usuario.get(url(codigo))
    assert r.status_code == 200
    assert r.content == CONTENIDO
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "bases.pdf" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-type"].startswith("application/pdf")
    assert bdns.llamadas == 1
    [doc] = await filas_documento(codigo)
    assert (doc.sha256, doc.tamano, doc.mime) == (SHA, len(CONTENIDO), "application/pdf")
    assert doc.fetched_at is not None
    assert [p.name for p in archivos(almacen_dir) if p.parent.name != "tmp"] == [SHA]


class AlmacenConUrl(AlmacenLocal):
    """Local store that, like the future S3 one, answers with a temporary URL; records what it was asked to force."""

    def __init__(self, raiz: Path) -> None:
        super().__init__(raiz)
        self.pedidos: list[tuple[str, str, str]] = []

    def url_temporal(self, clave: str, *, mime: str, content_disposition: str) -> str | None:
        self.pedidos.append((clave, mime, content_disposition))
        return "https://bucket.example/firmada"


async def test_la_redireccion_a_url_temporal_fuerza_tipo_seguro_y_descarga(
    client_usuario: AsyncClient, bdns: Bdns, almacen_dir: Path
) -> None:
    """The BDNS says image/svg+xml: the signed URL must not serve it as a page (it overrides type and disposition)."""
    bdns.respuesta = lambda: httpx.Response(200, content=b"<svg onload=x>", headers={"content-type": "image/svg+xml"})
    almacen = AlmacenConUrl(almacen_dir)
    app.dependency_overrides[documentos.almacen] = lambda: almacen
    codigo = await convocatoria_con_documento()
    r = await client_usuario.get(url(codigo), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://bucket.example/firmada"
    assert r.headers["cache-control"] == "private, no-store"
    [(_, mime, disposicion)] = almacen.pedidos
    assert mime == "application/octet-stream"
    assert disposicion.startswith("attachment;") and "bases.pdf" in disposicion


async def test_segunda_peticion_sale_del_almacen_para_otro_usuario(
    client_usuario: AsyncClient, bdns: Bdns, empresa: dict
) -> None:
    codigo = await convocatoria_con_documento()
    await client_usuario.get(url(codigo))
    otro = await crear_sesion_en_bd(empresa["id"], "usuario")
    async with await cliente_de(otro) as c2:
        r = await c2.get(url(codigo))
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == SHA
    assert bdns.llamadas == 1


async def test_fallo_de_bdns_es_503_sin_archivo_y_luego_se_reintenta(
    client_usuario: AsyncClient, bdns: Bdns, almacen_dir: Path
) -> None:
    codigo = await convocatoria_con_documento()
    ok = bdns.respuesta
    bdns.respuesta = lambda: httpx.Response(503)
    r = await client_usuario.get(url(codigo))
    assert r.status_code == 503
    assert archivos(almacen_dir) == []
    assert all(d.fetched_at is None for d in await filas_documento(codigo))
    bdns.respuesta = ok
    assert (await client_usuario.get(url(codigo))).status_code == 200


async def test_html_del_waf_es_503(client_usuario: AsyncClient, bdns: Bdns, almacen_dir: Path) -> None:
    codigo = await convocatoria_con_documento()
    bdns.respuesta = lambda: httpx.Response(200, text="<html>bloqueado</html>", headers={"content-type": "text/html"})
    assert (await client_usuario.get(url(codigo))).status_code == 503
    assert archivos(almacen_dir) == []


async def test_documento_demasiado_grande_es_413_sin_archivo(
    client_usuario: AsyncClient, almacen_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codigo = await convocatoria_con_documento()
    monkeypatch.setattr(documentos, "max_bytes", lambda: 10)
    r = await client_usuario.get(url(codigo))
    assert r.status_code == 413
    assert r.json()["detail"]["codigo"] == "documento_demasiado_grande"
    assert archivos(almacen_dir) == []
    assert all(d.fetched_at is None for d in await filas_documento(codigo))


async def test_id_desconocido_o_convocatoria_desconocida_es_404_sin_llamadas(
    client_usuario: AsyncClient, bdns: Bdns
) -> None:
    codigo = await convocatoria_con_documento()
    assert (await client_usuario.get(url(codigo, 999))).status_code == 404
    assert (await client_usuario.get(url("TSTNOEXISTE", ID_DOC))).status_code == 404
    assert bdns.llamadas == 0


async def test_archivo_borrado_del_almacen_se_vuelve_a_bajar(
    client_usuario: AsyncClient, bdns: Bdns, almacen_dir: Path
) -> None:
    codigo = await convocatoria_con_documento()
    await client_usuario.get(url(codigo))
    for p in archivos(almacen_dir):
        p.unlink()
    r = await client_usuario.get(url(codigo))
    assert r.status_code == 200 and r.content == CONTENIDO
    assert bdns.llamadas == 2


async def test_anonimo_401_sin_evento_ni_llamadas(client: AsyncClient, bdns: Bdns) -> None:
    codigo = await convocatoria_con_documento()
    assert (await client.get(url(codigo))).status_code == 401
    assert bdns.llamadas == 0


# --- Access events and metrics -------------------------------------------------


async def test_eventos_y_metricas(
    client_usuario: AsyncClient,
    client_admin: AsyncClient,
    usuario_raso: Sesion,
    empresa: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    codigo = await convocatoria_con_documento()
    await client_usuario.get(url(codigo))
    await client_usuario.get(url(codigo))
    b = await crear_sesion_en_bd(empresa["id"], "usuario")
    async with await cliente_de(b) as c2:
        await c2.get(url(codigo))

    assert await eventos([usuario_raso.id, b.id]) == [
        (usuario_raso.id, "bdns"),
        (usuario_raso.id, "cache"),
        (b.id, "cache"),
    ]
    texto_logs = " ".join(r.getMessage() for r in caplog.records)
    for secreto in (usuario_raso.email, b.email, CONTENIDO.decode()):
        assert secreto not in texto_logs

    r = await client_admin.get("/admin/documentos/metricas")
    assert r.status_code == 200
    ours = [i for i in r.json()["items"] if i["convocatoria_codigo"] == codigo]
    assert len(ours) == 1
    assert (ours[0]["bdns_documento_id"], ours[0]["accesos"], ours[0]["usuarios_distintos"]) == (ID_DOC, 3, 2)
    assert ours[0]["ultimo_acceso"] is not None
    cuerpo = r.text
    assert usuario_raso.email not in cuerpo and b.email not in cuerpo


async def test_metricas_solo_admin(client_usuario: AsyncClient, client: AsyncClient) -> None:
    assert (await client_usuario.get("/admin/documentos/metricas")).status_code == 403
    assert (await client.get("/admin/documentos/metricas")).status_code == 401


async def test_metricas_rango_de_fechas(client_usuario: AsyncClient, client_admin: AsyncClient) -> None:
    codigo = await convocatoria_con_documento()
    await client_usuario.get(url(codigo))
    futuro = await client_admin.get("/admin/documentos/metricas", params={"desde": "2999-01-01"})
    assert [i for i in futuro.json()["items"] if i["convocatoria_codigo"] == codigo] == []
    hoy = await client_admin.get("/admin/documentos/metricas", params={"desde": "2000-01-01", "hasta": "2999-01-01"})
    assert [i for i in hoy.json()["items"] if i["convocatoria_codigo"] == codigo]


# --- Detail carries our URL ----------------------------------------------------


async def test_el_detalle_expone_la_url_propia(client_usuario: AsyncClient) -> None:
    codigo = await convocatoria_con_documento()
    r = await client_usuario.get(f"/convocatorias/{codigo}")
    docs: list[dict[str, Any]] = r.json()["documentos"]
    assert docs[0]["url"] == f"/convocatorias/{codigo}/documentos/{ID_DOC}"
    assert "bdns" not in docs[0]["url"].lower().replace("convocatorias", "")
