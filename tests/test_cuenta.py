"""Account: notice preferences (N9a), password change, phone and the 12-character policy (N11)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.limite_intentos import LimiteIntentos, cambio_de_password
from app.database import AsyncSessionLocal
from app.main import app
from tests.conftest import PASSWORD_TEST, Sesion, crear_sesion_en_bd
from tests.migraciones import base_temporal

NUEVA = "una-clave-nueva-larga-1"
TABLA = text("SELECT 1 FROM pg_tables WHERE tablename = 'usuario_preferencia_aviso'")
COLUMNA = text("SELECT 1 FROM information_schema.columns WHERE column_name = 'telefono'")
DEFECTOS = [
    {"tipo": "nuevas", "email": True, "app": True},
    {"tipo": "plazos", "email": True, "app": True},
    {"tipo": "alertas", "email": True, "app": False},
    {"tipo": "semanal", "email": True, "app": False},
    {"tipo": "seguimiento", "email": False, "app": True},
]


# --- preferences ---------------------------------------------------------------------------------------------


async def test_preferences_require_login(client: AsyncClient) -> None:
    assert (await client.get("/me/preferencias-aviso")).status_code == 401
    assert (await client.put("/me/preferencias-aviso", json=[])).status_code == 401


async def test_defaults_are_the_five_kinds_of_the_design(client_usuario: AsyncClient) -> None:
    r = await client_usuario.get("/me/preferencias-aviso")
    assert r.status_code == 200 and r.json() == DEFECTOS


async def test_put_replaces_what_it_sends_and_keeps_the_rest(client_usuario: AsyncClient) -> None:
    r = await client_usuario.put("/me/preferencias-aviso", json=[{"tipo": "alertas", "email": False, "app": True}])
    assert r.status_code == 200 and len(r.json()) == 5
    assert r.json()[2] == {"tipo": "alertas", "email": False, "app": True}
    assert r.json()[0] == DEFECTOS[0]
    r = await client_usuario.put("/me/preferencias-aviso", json=[{"tipo": "alertas", "email": True, "app": True}])
    assert r.json()[2] == {"tipo": "alertas", "email": True, "app": True}
    assert (await client_usuario.get("/me/preferencias-aviso")).json() == r.json()


@pytest.mark.parametrize(
    "cuerpo",
    [
        [],
        [{"tipo": "otro", "email": True, "app": True}],
        [{"tipo": "nuevas", "email": True, "app": True}, {"tipo": "nuevas", "email": False, "app": False}],
        [{"tipo": "nuevas", "email": "si", "app": True}],
        [{"tipo": "nuevas", "email": True}],
        {"tipo": "nuevas", "email": True, "app": True},
        DEFECTOS + [{"tipo": "nuevas", "email": True, "app": True}],
    ],
)
async def test_put_validation(client_usuario: AsyncClient, cuerpo: object) -> None:
    assert (await client_usuario.put("/me/preferencias-aviso", json=cuerpo)).status_code == 422


async def test_preferences_are_personal(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    await client_usuario.put("/me/preferencias-aviso", json=[{"tipo": "nuevas", "email": False, "app": False}])
    otro = await crear_sesion_en_bd(usuario_raso.empresa_id, "usuario")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=otro.headers) as c:
        assert (await c.get("/me/preferencias-aviso")).json() == DEFECTOS


async def test_db_check_refuses_unknown_kind(usuario_raso: Sesion) -> None:
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="ck_usuario_preferencia_aviso_tipo"):
            await db.execute(
                text("INSERT INTO usuario_preferencia_aviso VALUES (:u, 'x', true, true, now(), now(), null, null)"),
                {"u": usuario_raso.id},
            )


# --- password change -----------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reiniciar_limite() -> None:
    cambio_de_password._fallos.clear()


async def _login(client: AsyncClient, email: str, password: str) -> int:
    return (await client.post("/auth/login", json={"email": email, "password": password})).status_code


async def test_change_password_requires_login(client: AsyncClient) -> None:
    r = await client.post("/auth/cambiar-password", json={"password_actual": "x", "password_nueva": NUEVA})
    assert r.status_code == 401


async def test_change_password_works_and_old_one_stops(
    client_usuario: AsyncClient, client: AsyncClient, usuario_raso: Sesion
) -> None:
    r = await client_usuario.post(
        "/auth/cambiar-password", json={"password_actual": PASSWORD_TEST, "password_nueva": NUEVA}
    )
    assert r.status_code == 204
    assert await _login(client, usuario_raso.email, NUEVA) == 200
    assert await _login(client, usuario_raso.email, PASSWORD_TEST) == 401


async def test_wrong_current_password_is_400_not_401(
    client_usuario: AsyncClient, client: AsyncClient, usuario_raso: Sesion
) -> None:
    r = await client_usuario.post(
        "/auth/cambiar-password", json={"password_actual": "no-es-la-actual", "password_nueva": NUEVA}
    )
    assert r.status_code == 400 and "actual" in r.json()["detail"]
    assert await _login(client, usuario_raso.email, PASSWORD_TEST) == 200  # nothing changed


async def test_same_password_is_rejected(client_usuario: AsyncClient) -> None:
    r = await client_usuario.post(
        "/auth/cambiar-password", json={"password_actual": PASSWORD_TEST, "password_nueva": PASSWORD_TEST}
    )
    assert r.status_code == 400


@pytest.mark.parametrize("nueva", ["a" * 11, "ñ" * 37, "a" * 73, ""])
async def test_new_password_policy(client_usuario: AsyncClient, nueva: str) -> None:
    r = await client_usuario.post(
        "/auth/cambiar-password", json={"password_actual": PASSWORD_TEST, "password_nueva": nueva}
    )
    assert r.status_code == 422


async def test_twelve_characters_are_enough(client_usuario: AsyncClient) -> None:
    r = await client_usuario.post(
        "/auth/cambiar-password", json={"password_actual": PASSWORD_TEST, "password_nueva": "a" * 12}
    )
    assert r.status_code == 204


async def test_repeated_failures_are_throttled_and_a_success_resets(client_usuario: AsyncClient) -> None:
    mala = {"password_actual": "mala", "password_nueva": NUEVA}
    for _ in range(5):
        assert (await client_usuario.post("/auth/cambiar-password", json=mala)).status_code == 400
    assert (await client_usuario.post("/auth/cambiar-password", json=mala)).status_code == 429
    buena = {"password_actual": PASSWORD_TEST, "password_nueva": NUEVA}
    assert (await client_usuario.post("/auth/cambiar-password", json=buena)).status_code == 429  # still blocked


def test_limiter_window_expires() -> None:
    ahora = [0.0]
    limite = LimiteIntentos(maximo=2, ventana=10, reloj=lambda: ahora[0])
    limite.registrar_fallo(1)
    limite.registrar_fallo(1)
    assert limite.bloqueado(1) and not limite.bloqueado(2)
    ahora[0] = 11
    assert not limite.bloqueado(1)
    limite.registrar_fallo(1)
    limite.olvidar(1)
    assert not limite.bloqueado(1)


async def test_users_can_no_longer_set_their_own_password_through_patch(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    r = await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"password": NUEVA})
    assert r.status_code == 403 and "cambiar-password" in r.json()["detail"]


async def test_a_manager_can_still_reset_a_colleagues_password_with_12_chars(
    client_gestor: AsyncClient, usuario_raso: Sesion
) -> None:
    url = f"/usuarios/{usuario_raso.id}"
    assert (await client_gestor.patch(url, json={"password": "corta-11-ch"})).status_code == 422
    assert (await client_gestor.patch(f"/usuarios/{usuario_raso.id}", json={"password": NUEVA})).status_code == 200


# --- phone ---------------------------------------------------------------------------------------------------


async def test_phone_is_part_of_the_profile_and_editable_by_the_owner(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    assert (await client_usuario.get("/auth/me")).json()["telefono"] is None
    r = await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"telefono": "  +34 600 123 456 "})
    assert r.status_code == 200 and r.json()["telefono"] == "+34 600 123 456"
    assert (await client_usuario.get("/auth/me")).json()["telefono"] == "+34 600 123 456"
    r = await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"telefono": ""})
    assert r.json()["telefono"] is None
    await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"telefono": "600123456"})
    r = await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"telefono": None})
    assert r.json()["telefono"] is None


@pytest.mark.parametrize("telefono", ["abc", "12345", "6001234567890123", "600; DROP", "+34 600 x", "1" * 31])
async def test_invalid_phone_is_422(client_usuario: AsyncClient, usuario_raso: Sesion, telefono: str) -> None:
    assert (await client_usuario.patch(f"/usuarios/{usuario_raso.id}", json={"telefono": telefono})).status_code == 422


async def test_db_check_refuses_bad_phone(usuario_raso: Sesion) -> None:
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="ck_usuario_telefono"):
            await db.execute(text("UPDATE usuario SET telefono = 'hola' WHERE id = :i"), {"i": usuario_raso.id})


async def test_migration_up_and_down() -> None:
    async with base_temporal() as base:
        base.alembic("upgrade", "head")
        async with base.motor.connect() as c:
            assert (await c.execute(TABLA)).first()
        base.alembic("downgrade", "b5d9e1c37a62")
        async with base.motor.connect() as c:
            assert not (await c.execute(TABLA)).first()
            assert not (await c.execute(COLUMNA)).first()
