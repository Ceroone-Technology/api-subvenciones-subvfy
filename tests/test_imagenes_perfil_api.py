"""Avatar (`/usuarios/.../avatar`) and company logo (`/empresas/{id}/logo`), plus `cargo` and `sitio_web`."""

import io
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.main import app
from app.services import imagenes_perfil
from app.services.almacen_documentos import AlmacenLocal
from tests.conftest import Sesion, crear_sesion_en_bd, nif_de_prueba

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'


def imagen(formato: str = "PNG", tam: tuple[int, int] = (600, 400)) -> bytes:
    salida = io.BytesIO()
    Image.new("RGB", tam, (10, 120, 200)).save(salida, formato)
    return salida.getvalue()


def adjunto(datos: bytes, nombre: str = "foto.png", tipo: str = "image/png") -> dict:
    return {"archivo": (nombre, datos, tipo)}


@pytest_asyncio.fixture(autouse=True)
async def almacen(tmp_path: Path) -> AsyncGenerator[Path, None]:
    app.dependency_overrides[imagenes_perfil.almacen] = lambda: AlmacenLocal(tmp_path / "almacen")
    yield tmp_path / "almacen"
    app.dependency_overrides.pop(imagenes_perfil.almacen, None)


# --- Avatar ---------------------------------------------------------------------------


async def test_subir_avatar_devuelve_url_versionada_y_se_sirve(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    r = await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen()))
    assert r.status_code == 200, r.text
    url = r.json()["avatar_url"]
    assert url.startswith(f"/usuarios/{usuario_raso.id}/avatar?v=")

    g = await client_usuario.get(url)
    assert g.status_code == 200
    assert g.headers["content-type"] == "image/webp"
    assert g.headers["x-content-type-options"] == "nosniff"
    img = Image.open(io.BytesIO(g.content))
    assert img.format == "WEBP" and max(img.size) == 256


async def test_avatar_aparece_en_me_y_en_usuario_read(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    assert (await client_usuario.get("/auth/me")).json()["avatar_url"] is None
    url = (await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen()))).json()["avatar_url"]
    assert (await client_usuario.get("/auth/me")).json()["avatar_url"] == url
    assert (await client_usuario.get(f"/usuarios/{usuario_raso.id}")).json()["avatar_url"] == url


async def test_cambiar_la_imagen_cambia_la_version(client_usuario: AsyncClient) -> None:
    a = (await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen(tam=(300, 300))))).json()["avatar_url"]
    b = (await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen("JPEG", (200, 100))))).json()[
        "avatar_url"
    ]
    assert a != b


async def test_quitar_avatar(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen()))
    assert (await client_usuario.delete("/usuarios/me/avatar")).status_code == 204
    assert (await client_usuario.get("/auth/me")).json()["avatar_url"] is None
    assert (await client_usuario.get(f"/usuarios/{usuario_raso.id}/avatar")).status_code == 404
    assert (await client_usuario.delete("/usuarios/me/avatar")).status_code == 204  # idempotent


async def test_avatar_sin_autenticar(client: AsyncClient) -> None:
    assert (await client.put("/usuarios/me/avatar", files=adjunto(imagen()))).status_code == 401
    assert (await client.delete("/usuarios/me/avatar")).status_code == 401
    assert (await client.get("/usuarios/1/avatar")).status_code == 401


async def test_el_avatar_de_otra_empresa_es_404_y_el_de_un_companero_no(
    client_usuario: AsyncClient, client_gestor: AsyncClient, otra_empresa: dict
) -> None:
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "usuario")
    assert (await client_usuario.get(f"/usuarios/{ajeno.id}/avatar")).status_code == 404
    await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen()))
    yo = (await client_usuario.get("/auth/me")).json()["id"]
    assert (await client_gestor.get(f"/usuarios/{yo}/avatar")).status_code == 200


async def test_el_admin_ve_cualquier_avatar(
    client_usuario: AsyncClient, client_admin: AsyncClient, usuario_raso: Sesion
) -> None:
    await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen()))
    assert (await client_admin.get(f"/usuarios/{usuario_raso.id}/avatar")).status_code == 200


async def test_get_avatar_con_etag_y_cache(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    url = (await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen()))).json()["avatar_url"]
    g = await client_usuario.get(url)
    assert "immutable" in g.headers["cache-control"] and "private" in g.headers["cache-control"]
    sin_version = await client_usuario.get(f"/usuarios/{usuario_raso.id}/avatar")
    assert "immutable" not in sin_version.headers["cache-control"]
    assert (await client_usuario.get(url, headers={"If-None-Match": g.headers["etag"]})).status_code == 304


@pytest.mark.parametrize(
    ("datos", "nombre", "tipo", "codigo"),
    [
        (SVG, "a.svg", "image/svg+xml", 415),
        (SVG, "a.png", "image/png", 415),  # lies about extension and header
        (b"%PDF-1.4", "a.png", "image/png", 415),
        (b"", "a.png", "image/png", 400),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "a.png", "image/png", 400),  # right magic, broken body
    ],
)
async def test_rechaza_lo_que_no_es_imagen_valida(
    client_usuario: AsyncClient, datos: bytes, nombre: str, tipo: str, codigo: int
) -> None:
    r = await client_usuario.put("/usuarios/me/avatar", files=adjunto(datos, nombre, tipo))
    assert r.status_code == codigo, r.text


async def test_acepta_png_con_cabecera_mentirosa(client_usuario: AsyncClient) -> None:
    r = await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen("PNG"), "x.svg", "image/svg+xml"))
    assert r.status_code == 200


async def test_rechaza_mas_de_2_mb(client_usuario: AsyncClient) -> None:
    r = await client_usuario.put(
        "/usuarios/me/avatar", files=adjunto(b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024))
    )
    assert r.status_code == 413


async def test_rechaza_una_bomba_de_descompresion(client_usuario: AsyncClient) -> None:
    r = await client_usuario.put("/usuarios/me/avatar", files=adjunto(imagen("PNG", (12000, 12000))))
    assert r.status_code == 400


async def test_sin_archivo_es_422(client_usuario: AsyncClient) -> None:
    assert (await client_usuario.put("/usuarios/me/avatar")).status_code == 422


# --- Logo -----------------------------------------------------------------------------


async def test_gestor_sube_y_quita_el_logo(client_gestor: AsyncClient, gestor: Sesion) -> None:
    r = await client_gestor.put(f"/empresas/{gestor.empresa_id}/logo", files=adjunto(imagen()))
    assert r.status_code == 200, r.text
    url = r.json()["logo_url"]
    assert url.startswith(f"/empresas/{gestor.empresa_id}/logo?v=")
    g = await client_gestor.get(url)
    assert g.status_code == 200 and g.headers["content-type"] == "image/webp"
    assert g.headers["x-content-type-options"] == "nosniff"
    assert (await client_gestor.get("/empresas/me")).json()["logo_url"] == url
    assert (await client_gestor.delete(f"/empresas/{gestor.empresa_id}/logo")).status_code == 204
    assert (await client_gestor.get("/empresas/me")).json()["logo_url"] is None
    assert (await client_gestor.get(f"/empresas/{gestor.empresa_id}/logo")).status_code == 404


async def test_el_usuario_raso_no_toca_el_logo_pero_lo_ve(
    client_usuario: AsyncClient, client_gestor: AsyncClient, gestor: Sesion
) -> None:
    await client_gestor.put(f"/empresas/{gestor.empresa_id}/logo", files=adjunto(imagen()))
    ruta = f"/empresas/{gestor.empresa_id}/logo"
    assert (await client_usuario.put(ruta, files=adjunto(imagen()))).status_code == 403
    assert (await client_usuario.delete(ruta)).status_code == 403
    assert (await client_usuario.get(ruta)).status_code == 200
    assert (await client_usuario.get("/auth/me")).json()["empresa"]["logo_url"] is not None


async def test_logo_de_otra_empresa_es_404(client_gestor: AsyncClient, otra_empresa: dict) -> None:
    ruta = f"/empresas/{otra_empresa['id']}/logo"
    assert (await client_gestor.put(ruta, files=adjunto(imagen()))).status_code == 404
    assert (await client_gestor.delete(ruta)).status_code == 404
    assert (await client_gestor.get(ruta)).status_code == 404


async def test_admin_gestiona_el_logo_de_cualquier_empresa(client_admin: AsyncClient, empresa: dict) -> None:
    assert (await client_admin.put(f"/empresas/{empresa['id']}/logo", files=adjunto(imagen()))).status_code == 200
    assert (await client_admin.delete(f"/empresas/{empresa['id']}/logo")).status_code == 204


async def test_logo_empresa_inexistente_es_404(client_admin: AsyncClient) -> None:
    assert (await client_admin.put("/empresas/999999999/logo", files=adjunto(imagen()))).status_code == 404


async def test_logo_sin_autenticar(client: AsyncClient) -> None:
    assert (await client.put("/empresas/1/logo", files=adjunto(imagen()))).status_code == 401


async def test_el_logo_rechaza_svg(client_gestor: AsyncClient, gestor: Sesion) -> None:
    r = await client_gestor.put(
        f"/empresas/{gestor.empresa_id}/logo", files=adjunto(SVG, "l.svg", "image/svg+xml")
    )
    assert r.status_code == 415


# --- cargo and sitio_web --------------------------------------------------------------


async def test_cargo_se_edita_en_el_propio_perfil(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    r = await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"cargo": "  Directora financiera "})
    assert r.status_code == 200, r.text
    assert r.json()["cargo"] == "Directora financiera"
    assert (await client_usuario.get("/auth/me")).json()["cargo"] == "Directora financiera"
    r = await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"cargo": ""})
    assert r.json()["cargo"] is None


async def test_cargo_largo_es_422(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    r = await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"cargo": "x" * 101})
    assert r.status_code == 422


async def test_sitio_web_en_la_empresa(client_gestor: AsyncClient, gestor: Sesion) -> None:
    ruta = f"/empresas/{gestor.empresa_id}"
    r = await client_gestor.patch(ruta, json={"sitio_web": " https://www.ejemplo.es/inicio "})
    assert r.status_code == 200, r.text
    assert r.json()["sitio_web"] == "https://www.ejemplo.es/inicio"
    assert (await client_gestor.get("/empresas/me")).json()["sitio_web"] == "https://www.ejemplo.es/inicio"
    assert (await client_gestor.patch(ruta, json={"sitio_web": None})).json()["sitio_web"] is None


@pytest.mark.parametrize(
    "valor",
    [
        "javascript:alert(1)",
        "ftp://ejemplo.es",
        "ejemplo.es",
        "//ejemplo.es",
        "https://",
        "http://exa mple.es",
        "data:text/html;base64,AAAA",
        "https://" + "a" * 200 + ".es",
    ],
)
async def test_sitio_web_invalido_es_422(client_gestor: AsyncClient, gestor: Sesion, valor: str) -> None:
    r = await client_gestor.patch(f"/empresas/{gestor.empresa_id}", json={"sitio_web": valor})
    assert r.status_code == 422, valor


async def test_la_base_rechaza_un_sitio_web_no_http() -> None:
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text("INSERT INTO empresa (razon_social, nif, sitio_web) VALUES ('X', :nif, 'javascript:alert(1)')"),
                {"nif": nif_de_prueba()},
            )
        await db.rollback()


async def test_el_login_funciona_con_cargo_y_avatar_nulos(client: AsyncClient, gestor: Sesion) -> None:
    from tests.conftest import PASSWORD_TEST

    r = await client.post("/auth/login", json={"email": gestor.email, "password": PASSWORD_TEST})
    assert r.status_code == 200, r.text
    assert r.json()["usuario"]["cargo"] is None and r.json()["usuario"]["avatar_url"] is None
