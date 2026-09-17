"""Autorización por rol y aislamiento entre empresas (Hito 2, Funcionalidad 4).

El bloque que más importa de toda la funcionalidad: Subvfy es multi-tenant,
y una fuga aquí significa que un cliente ve los datos de otro.
"""

import pytest
from httpx import AsyncClient

from tests.conftest import Sesion, crear_sesion_en_bd, email_de_prueba, nif_de_prueba


def _payload_usuario(empresa_id: int, rol_id: int) -> dict:
    return {
        "empresa_id": empresa_id,
        "rol_id": rol_id,
        "nombre": "Nuevo",
        "apellidos": "Usuario",
        "email": email_de_prueba("alta"),
        "password": "password-valida-123",
    }


# --- Aislamiento entre empresas -------------------------------------------------


@pytest.mark.asyncio
async def test_gestor_solo_ve_su_empresa_en_el_listado(
    client_gestor: AsyncClient, empresa: dict, otra_empresa: dict
) -> None:
    respuesta = await client_gestor.get("/empresas")
    assert respuesta.status_code == 200
    ids = [e["id"] for e in respuesta.json()["items"]]
    assert ids == [empresa["id"]]
    assert otra_empresa["id"] not in ids


@pytest.mark.asyncio
async def test_gestor_no_accede_a_otra_empresa(
    client_gestor: AsyncClient, otra_empresa: dict
) -> None:
    """404 y no 403: confirmar con un 403 que ese id existe ya sería filtrar
    información de otro cliente."""
    assert (await client_gestor.get(f"/empresas/{otra_empresa['id']}")).status_code == 404
    assert (
        await client_gestor.patch(f"/empresas/{otra_empresa['id']}", json={"sector": "X"})
    ).status_code == 404


@pytest.mark.asyncio
async def test_admin_ve_todas_las_empresas(
    client_admin: AsyncClient, empresa: dict, otra_empresa: dict
) -> None:
    respuesta = await client_admin.get("/empresas")
    ids = [e["id"] for e in respuesta.json()["items"]]
    assert empresa["id"] in ids
    assert otra_empresa["id"] in ids


@pytest.mark.asyncio
async def test_empresa_id_ajeno_en_el_listado_de_usuarios_se_ignora(
    client_gestor: AsyncClient, empresa: dict, otra_empresa: dict
) -> None:
    """Pedir explícitamente otra empresa no la devuelve: el filtro se impone
    con la empresa del token, no se toma del query param."""
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "usuario")

    respuesta = await client_gestor.get("/usuarios", params={"empresa_id": otra_empresa["id"]})
    assert respuesta.status_code == 200
    ids = [u["id"] for u in respuesta.json()["items"]]
    assert ajeno.id not in ids
    assert all(u["empresa_id"] == empresa["id"] for u in respuesta.json()["items"])


@pytest.mark.asyncio
async def test_usuario_de_otra_empresa_no_es_visible(
    client_gestor: AsyncClient, otra_empresa: dict
) -> None:
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "usuario")
    assert (await client_gestor.get(f"/usuarios/{ajeno.id}")).status_code == 404
    assert (
        await client_gestor.patch(f"/usuarios/{ajeno.id}", json={"nombre": "Robado"})
    ).status_code == 404
    assert (await client_gestor.delete(f"/usuarios/{ajeno.id}")).status_code == 404


# --- Permisos sobre empresas ----------------------------------------------------


@pytest.mark.asyncio
async def test_solo_admin_crea_empresas(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.post(
        "/empresas", json={"razon_social": "Intento SL", "nif": nif_de_prueba()}
    )
    assert respuesta.status_code == 403


@pytest.mark.asyncio
async def test_solo_admin_da_de_baja_empresas(client_gestor: AsyncClient, empresa: dict) -> None:
    assert (await client_gestor.delete(f"/empresas/{empresa['id']}")).status_code == 403


@pytest.mark.asyncio
async def test_gestor_edita_su_propia_empresa(client_gestor: AsyncClient, empresa: dict) -> None:
    respuesta = await client_gestor.patch(f"/empresas/{empresa['id']}", json={"sector": "Logística"})
    assert respuesta.status_code == 200
    assert respuesta.json()["sector"] == "Logística"


@pytest.mark.asyncio
async def test_usuario_raso_no_edita_su_empresa(client_usuario: AsyncClient, empresa: dict) -> None:
    assert (
        await client_usuario.patch(f"/empresas/{empresa['id']}", json={"sector": "X"})
    ).status_code == 403


@pytest.mark.asyncio
async def test_usuario_raso_consulta_su_empresa(client_usuario: AsyncClient, empresa: dict) -> None:
    respuesta = await client_usuario.get(f"/empresas/{empresa['id']}")
    assert respuesta.status_code == 200


# --- Permisos sobre usuarios ----------------------------------------------------


@pytest.mark.asyncio
async def test_gestor_da_de_alta_en_su_empresa(
    client_gestor: AsyncClient, empresa: dict, rol_usuario_id: int
) -> None:
    respuesta = await client_gestor.post(
        "/usuarios", json=_payload_usuario(empresa["id"], rol_usuario_id)
    )
    assert respuesta.status_code == 201, respuesta.text


@pytest.mark.asyncio
async def test_gestor_no_da_de_alta_en_otra_empresa(
    client_gestor: AsyncClient, otra_empresa: dict, rol_usuario_id: int
) -> None:
    respuesta = await client_gestor.post(
        "/usuarios", json=_payload_usuario(otra_empresa["id"], rol_usuario_id)
    )
    assert respuesta.status_code == 404


@pytest.mark.asyncio
async def test_usuario_raso_no_da_de_alta(
    client_usuario: AsyncClient, empresa: dict, rol_usuario_id: int
) -> None:
    respuesta = await client_usuario.post(
        "/usuarios", json=_payload_usuario(empresa["id"], rol_usuario_id)
    )
    assert respuesta.status_code == 403


@pytest.mark.asyncio
async def test_usuario_raso_edita_su_propio_perfil(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    respuesta = await client_usuario.patch(
        f"/usuarios/{usuario_raso.id}", json={"nombre": "Nombre Nuevo"}
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["nombre"] == "Nombre Nuevo"


@pytest.mark.asyncio
async def test_usuario_raso_no_edita_a_un_companero(
    client_usuario: AsyncClient, empresa: dict
) -> None:
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    respuesta = await client_usuario.patch(f"/usuarios/{companero.id}", json={"nombre": "X"})
    assert respuesta.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("campo", ["rol_id", "estado", "empresa_id"])
async def test_usuario_raso_no_se_asciende_a_si_mismo(
    client_usuario: AsyncClient, usuario_raso: Sesion, otra_empresa: dict, campo: str
) -> None:
    """La escalada de privilegios más barata sería un PATCH de tu propio
    rol_id. También se bloquea cambiarse de empresa o reactivarse solo."""
    valores = {"rol_id": 1, "estado": "activo", "empresa_id": otra_empresa["id"]}
    respuesta = await client_usuario.patch(
        f"/usuarios/{usuario_raso.id}", json={campo: valores[campo]}
    )
    assert respuesta.status_code == 403
    assert campo in respuesta.json()["detail"]


@pytest.mark.asyncio
async def test_usuario_raso_no_da_de_baja(client_usuario: AsyncClient, empresa: dict) -> None:
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    assert (await client_usuario.delete(f"/usuarios/{companero.id}")).status_code == 403


@pytest.mark.asyncio
async def test_gestor_da_de_baja_en_su_empresa(client_gestor: AsyncClient, empresa: dict) -> None:
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    assert (await client_gestor.delete(f"/usuarios/{companero.id}")).status_code == 204


# --- Catálogo de roles ----------------------------------------------------------


@pytest.mark.asyncio
async def test_cualquier_autenticado_lee_el_catalogo_de_roles(client_usuario: AsyncClient) -> None:
    respuesta = await client_usuario.get("/roles")
    assert respuesta.status_code == 200
    assert len(respuesta.json()) == 3
