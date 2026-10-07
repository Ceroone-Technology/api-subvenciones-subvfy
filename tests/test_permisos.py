"""Autorización por rol y aislamiento entre empresas (Hito 2, Funcionalidad 4).

El bloque que más importa de toda la funcionalidad: Subvfy es multi-tenant,
y una fuga aquí significa que un cliente ve los datos de otro.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.conftest import (
    PASSWORD_TEST,
    Sesion,
    crear_sesion_en_bd,
    email_de_prueba,
    nif_de_prueba,
)


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


# --- Escalada de privilegios en usuarios (AUD-001 y AUD-002) --------------------
#
# Reglas acordadas en el ticket 1:
#   - `rol_id` y `empresa_id` son solo de admin.
#   - Un gestor gestiona únicamente objetivos con rol `usuario` de su empresa,
#     más su propio perfil.
#   - Nadie se cambia su propio rol ni su propio estado, admin incluido.
#   - El tenant se comprueba antes que el rol: otra empresa da 404; el propio
#     tenant con rol insuficiente, 403.


@pytest.mark.asyncio
async def test_un_gestor_no_se_cambia_el_rol(
    client_gestor: AsyncClient, gestor: Sesion, rol_admin_id: int
) -> None:
    """La escalada de AUD-001: con el rol releído de la BD en cada petición,
    un `rol_id` de admin aquí convertía al gestor en admin global."""
    respuesta = await client_gestor.patch(f"/usuarios/{gestor.id}", json={"rol_id": rol_admin_id})
    assert respuesta.status_code == 403
    assert "rol_id" in respuesta.json()["detail"]

    # Y sigue viendo solo su empresa.
    assert (await client_gestor.get("/empresas")).json()["total"] == 1


@pytest.mark.asyncio
async def test_un_gestor_no_asciende_a_nadie(
    client_gestor: AsyncClient, usuario_raso: Sesion, rol_admin_id: int, rol_gestor_id: int
) -> None:
    for rol_id in (rol_admin_id, rol_gestor_id):
        respuesta = await client_gestor.patch(f"/usuarios/{usuario_raso.id}", json={"rol_id": rol_id})
        assert respuesta.status_code == 403, f"rol_id={rol_id}: {respuesta.text}"


@pytest.mark.asyncio
async def test_un_gestor_no_toca_empresa_id_ni_la_suya(
    client_gestor: AsyncClient, gestor: Sesion, usuario_raso: Sesion, empresa: dict, otra_empresa: dict
) -> None:
    """`empresa_id` es de admin: ni el propio ni el de otro, ni siquiera con el
    valor que ya tienen."""
    propio = await client_gestor.patch(f"/usuarios/{gestor.id}", json={"empresa_id": empresa["id"]})
    ajeno = await client_gestor.patch(f"/usuarios/{usuario_raso.id}", json={"empresa_id": empresa["id"]})
    assert propio.status_code == 403, propio.text
    assert ajeno.status_code == 403, ajeno.text


@pytest.mark.asyncio
async def test_un_gestor_solo_crea_usuarios_rasos(
    client_gestor: AsyncClient, empresa: dict, rol_admin_id: int, rol_gestor_id: int, rol_usuario_id: int
) -> None:
    for rol_id in (rol_admin_id, rol_gestor_id):
        respuesta = await client_gestor.post("/usuarios", json=_payload_usuario(empresa["id"], rol_id))
        assert respuesta.status_code == 403, f"rol_id={rol_id}: {respuesta.text}"

    # El alta normal sigue funcionando.
    valida = await client_gestor.post("/usuarios", json=_payload_usuario(empresa["id"], rol_usuario_id))
    assert valida.status_code == 201, valida.text


@pytest.mark.asyncio
async def test_un_gestor_no_gestiona_al_admin_de_su_empresa(
    client_gestor: AsyncClient, admin_de_la_empresa: Sesion, client: AsyncClient
) -> None:
    """AUD-002: el admin que comparte empresa con el gestor era editable por él,
    contraseña incluida, lo que permitía tomar su cuenta."""
    editar = await client_gestor.patch(f"/usuarios/{admin_de_la_empresa.id}", json={"nombre": "Secuestrado"})
    password = await client_gestor.patch(
        f"/usuarios/{admin_de_la_empresa.id}", json={"password": "la-del-gestor-123"}
    )
    baja = await client_gestor.delete(f"/usuarios/{admin_de_la_empresa.id}")
    assert editar.status_code == 403, editar.text
    assert password.status_code == 403, password.text
    assert baja.status_code == 403, baja.text

    # Su cuenta sigue intacta: entra con su contraseña de siempre.
    login = await client.post(
        "/auth/login", json={"email": admin_de_la_empresa.email, "password": PASSWORD_TEST}
    )
    assert login.status_code == 200, "la contraseña del admin no debería haber cambiado"


@pytest.mark.asyncio
async def test_un_gestor_no_gestiona_a_otro_gestor(
    client_gestor: AsyncClient, otro_gestor: Sesion
) -> None:
    editar = await client_gestor.patch(f"/usuarios/{otro_gestor.id}", json={"nombre": "Editado"})
    baja = await client_gestor.delete(f"/usuarios/{otro_gestor.id}")
    assert editar.status_code == 403, editar.text
    assert baja.status_code == 403, baja.text


@pytest.mark.asyncio
async def test_un_admin_no_se_degrada_ni_se_bloquea(
    client_admin: AsyncClient, admin: Sesion, rol_usuario_id: int
) -> None:
    """Autoprotección: si el único admin se degrada o se bloquea, ya no hay
    quien lo arregle por la API."""
    rol = await client_admin.patch(f"/usuarios/{admin.id}", json={"rol_id": rol_usuario_id})
    estado = await client_admin.patch(f"/usuarios/{admin.id}", json={"estado": "bloqueado"})
    baja = await client_admin.delete(f"/usuarios/{admin.id}")
    assert rol.status_code == 403, rol.text
    assert estado.status_code == 403, estado.text
    assert baja.status_code == 403, baja.text


@pytest.mark.asyncio
async def test_un_gestor_de_otra_empresa_recibe_404_no_403(
    admin_de_la_empresa: Sesion, otra_empresa: dict
) -> None:
    """El tenant se comprueba primero: un 403 confirmaría que ese id existe."""
    forastero = await crear_sesion_en_bd(otra_empresa["id"], "gestor")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=forastero.headers
    ) as cliente:
        editar = await cliente.patch(f"/usuarios/{admin_de_la_empresa.id}", json={"nombre": "X"})
        baja = await cliente.delete(f"/usuarios/{admin_de_la_empresa.id}")
    assert editar.status_code == 404, editar.text
    assert baja.status_code == 404, baja.text


# --- Lo que debe seguir funcionando igual --------------------------------------


@pytest.mark.asyncio
async def test_un_gestor_sigue_gestionando_a_un_usuario_raso(
    client_gestor: AsyncClient, usuario_raso: Sesion
) -> None:
    nombre = await client_gestor.patch(f"/usuarios/{usuario_raso.id}", json={"nombre": "Renombrado"})
    estado = await client_gestor.patch(f"/usuarios/{usuario_raso.id}", json={"estado": "bloqueado"})
    password = await client_gestor.patch(f"/usuarios/{usuario_raso.id}", json={"password": "otra-valida-123"})
    baja = await client_gestor.delete(f"/usuarios/{usuario_raso.id}")
    assert nombre.status_code == 200, nombre.text
    assert estado.status_code == 200, estado.text
    assert password.status_code == 200, password.text
    assert baja.status_code == 204, baja.text


@pytest.mark.asyncio
async def test_un_admin_sigue_moviendo_rol_y_empresa_de_otros(
    client_admin: AsyncClient, usuario_raso: Sesion, otra_empresa: dict, rol_gestor_id: int
) -> None:
    rol = await client_admin.patch(f"/usuarios/{usuario_raso.id}", json={"rol_id": rol_gestor_id})
    empresa = await client_admin.patch(
        f"/usuarios/{usuario_raso.id}", json={"empresa_id": otra_empresa["id"]}
    )
    assert rol.status_code == 200, rol.text
    assert empresa.status_code == 200, empresa.text
