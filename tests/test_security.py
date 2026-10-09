"""Red de seguridad del cambio de librerías de autenticación (AUD-004).

Escrita **antes** de sustituir python-jose por PyJWT y passlib por bcrypt
directo, y en verde con las librerías antiguas. Lo que fija:

- Los hashes ya guardados siguen verificando sin migrar datos. Los dos
  literales los generó passlib 1.7.4 (bcrypt 4.2.1) con `hashear_password`.
- El de más de 72 bytes protege a quien se dio de alta con una contraseña
  multibyte larga: passlib la truncaba en silencio a 72 bytes, así que su hash
  es el de esos 72 bytes y tiene que seguir entrando con la contraseña entera.
- Los tokens conservan algoritmo y claims. La cabecera y el payload se leen
  decodificando el base64 a mano, sin pasar por ninguna librería JWT, para que
  el test no dependa de la que se está cambiando.
- Un token caducado, manipulado o firmado con otro algoritmo da 401. Se
  fabrican con `hmac` de la biblioteca estándar, por el mismo motivo.
"""

import base64
import hashlib
import hmac
import json
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app import cli
from app.config import settings
from app.core.security import (
    TIPO_ACCESS,
    TIPO_REFRESH,
    TokenInvalido,
    crear_access_token,
    crear_refresh_token,
    hashear_password,
    leer_token,
    verificar_password,
)
from app.database import AsyncSessionLocal
from app.models import Usuario
from tests.conftest import Sesion, crear_sesion_en_bd

PASSWORD_NORMAL = "password-de-prueba"
HASH_PASSLIB_NORMAL = "$2b$12$kdVoC1pZxZ.KguTLp0xS5ebS1ccSfpwA8Vq7.6IgQwtB7FKmcMmiW"

PASSWORD_LARGA_MULTIBYTE = "ñ" * 40  # 40 caracteres, 80 bytes en UTF-8
HASH_PASSLIB_LARGA_MULTIBYTE = "$2b$12$rqFBXUMjGKE9WD3r2.7N7eZrqX7QkJSKWSN55Z6z0yZUzq6J1cOsi"


def _b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _desde_b64url(segmento: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segmento + "=" * (-len(segmento) % 4)))


def _token_a_mano(cabecera: dict, payload: dict, firma: bytes | None = None, clave: str | None = None) -> str:
    """JWT montado con la biblioteca estándar. Si no se da la firma, se firma
    con HMAC-SHA256 o SHA512 según la `alg` de la cabecera."""
    inicio = f"{_b64url(json.dumps(cabecera).encode())}.{_b64url(json.dumps(payload).encode())}"
    if firma is None:
        digest = {"HS256": hashlib.sha256, "HS512": hashlib.sha512}[cabecera["alg"]]
        firma = hmac.new((clave or settings.jwt_secret_key).encode(), inicio.encode(), digest).digest()
    return f"{inicio}.{_b64url(firma)}"


def _claims_validos(usuario_id: int) -> dict:
    ahora = int(time.time())
    return {"sub": str(usuario_id), "tipo": TIPO_ACCESS, "iat": ahora, "exp": ahora + 3600}


# --- Hashes ya guardados -----------------------------------------------------


def test_un_hash_de_passlib_sigue_verificando() -> None:
    assert verificar_password(PASSWORD_NORMAL, HASH_PASSLIB_NORMAL)
    assert not verificar_password("otra-password", HASH_PASSLIB_NORMAL)


def test_un_hash_de_passlib_de_mas_de_72_bytes_sigue_verificando_con_la_password_entera() -> None:
    assert len(PASSWORD_LARGA_MULTIBYTE.encode()) > 72
    assert verificar_password(PASSWORD_LARGA_MULTIBYTE, HASH_PASSLIB_LARGA_MULTIBYTE)
    assert not verificar_password("ñ" * 35, HASH_PASSLIB_LARGA_MULTIBYTE)


# --- Forma de los tokens -----------------------------------------------------


@pytest.mark.parametrize(
    ("crear", "tipo", "duracion"),
    [
        (crear_access_token, TIPO_ACCESS, settings.access_token_expire_minutes * 60),
        (crear_refresh_token, TIPO_REFRESH, settings.refresh_token_expire_days * 86400),
    ],
)
def test_el_token_conserva_algoritmo_y_claims(crear, tipo: str, duracion: int) -> None:
    token = crear(42)
    cabecera_b64, payload_b64, _ = token.split(".")
    assert _desde_b64url(cabecera_b64) == {"alg": "HS256", "typ": "JWT"}

    payload = _desde_b64url(payload_b64)
    assert set(payload) == {"sub", "tipo", "iat", "exp"}
    assert payload["sub"] == "42"
    assert payload["tipo"] == tipo
    assert isinstance(payload["iat"], int) and isinstance(payload["exp"], int)
    assert payload["exp"] - payload["iat"] == duracion

    assert leer_token(token, tipo) == 42


def test_un_token_a_mano_con_los_mismos_claims_se_acepta() -> None:
    """Control de los helpers de abajo: si este no pasara, los 401 no
    demostrarían nada."""
    assert leer_token(_token_a_mano({"alg": "HS256", "typ": "JWT"}, _claims_validos(42)), TIPO_ACCESS) == 42


# --- Tokens que no valen -----------------------------------------------------


def _manipulado(usuario_id: int) -> str:
    """Token válido de otro usuario al que se le cambia el `sub` sin refirmar."""
    cabecera, _, firma = crear_access_token(usuario_id + 1).split(".")
    payload = _b64url(json.dumps(_claims_validos(usuario_id)).encode())
    return f"{cabecera}.{payload}.{firma}"


def _caducado(usuario_id: int) -> str:
    claims = _claims_validos(usuario_id) | {"iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600}
    return _token_a_mano({"alg": "HS256", "typ": "JWT"}, claims)


def _hs512(usuario_id: int) -> str:
    return _token_a_mano({"alg": "HS512", "typ": "JWT"}, _claims_validos(usuario_id))


def _alg_none(usuario_id: int) -> str:
    return _token_a_mano({"alg": "none", "typ": "JWT"}, _claims_validos(usuario_id), firma=b"")


def _otra_clave(usuario_id: int) -> str:
    return _token_a_mano(
        {"alg": "HS256", "typ": "JWT"}, _claims_validos(usuario_id), clave="otra-clave-de-32-caracteres-o-mas-0000"
    )


TOKENS_QUE_NO_VALEN = [_caducado, _manipulado, _hs512, _alg_none, _otra_clave]


@pytest.mark.parametrize("fabricar", TOKENS_QUE_NO_VALEN)
def test_leer_token_rechaza(fabricar) -> None:
    with pytest.raises(TokenInvalido):
        leer_token(fabricar(42), TIPO_ACCESS)


@pytest.mark.parametrize("fabricar", TOKENS_QUE_NO_VALEN)
async def test_el_token_que_no_vale_da_401(client: AsyncClient, gestor: Sesion, fabricar) -> None:
    respuesta = await client.get("/auth/me", headers={"Authorization": f"Bearer {fabricar(gestor.id)}"})
    assert respuesta.status_code == 401


# --- Contraseñas de más de 72 bytes (bcrypt directo) -------------------------
# Desde aquí, tests añadidos con el cambio a bcrypt 5: fijan lo nuevo, no lo
# que ya hacía passlib.

PASSWORD_72_BYTES = "ñ" * 36


def test_un_hash_nuevo_tiene_la_forma_de_los_de_passlib() -> None:
    nuevo = hashear_password(PASSWORD_NORMAL)
    assert nuevo.startswith("$2b$12$") and len(nuevo) == len(HASH_PASSLIB_NORMAL)
    assert verificar_password(PASSWORD_NORMAL, nuevo)


def test_hashear_no_corta_y_lanza_con_mas_de_72_bytes() -> None:
    with pytest.raises(ValueError):
        hashear_password(PASSWORD_LARGA_MULTIBYTE)


def test_verificar_corta_a_72_bytes_como_passlib() -> None:
    assert verificar_password(PASSWORD_LARGA_MULTIBYTE, hashear_password(PASSWORD_72_BYTES))


async def test_un_alta_de_mas_de_72_bytes_da_422_aunque_quepa_en_72_caracteres(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    datos = {
        "empresa_id": empresa["id"],
        "rol_id": rol_usuario_id,
        "nombre": "Ana",
        "apellidos": "García López",
        "email": email_unico,
        "password": PASSWORD_LARGA_MULTIBYTE,
    }
    respuesta = await client_admin.post("/usuarios", json=datos)
    assert respuesta.status_code == 422
    assert "72 bytes" in respuesta.text


async def test_un_alta_de_72_bytes_justos_se_acepta(
    client_admin: AsyncClient, empresa: dict, rol_usuario_id: int, email_unico: str
) -> None:
    datos = {
        "empresa_id": empresa["id"],
        "rol_id": rol_usuario_id,
        "nombre": "Ana",
        "apellidos": "García López",
        "email": email_unico,
        "password": PASSWORD_72_BYTES,
    }
    assert (await client_admin.post("/usuarios", json=datos)).status_code == 201
    login = await client_admin.post("/auth/login", json={"email": email_unico, "password": PASSWORD_72_BYTES})
    assert login.status_code == 200


async def test_un_cambio_de_password_de_mas_de_72_bytes_da_422(
    client_admin: AsyncClient, usuario_raso: Sesion
) -> None:
    respuesta = await client_admin.patch(f"/usuarios/{usuario_raso.id}", json={"password": PASSWORD_LARGA_MULTIBYTE})
    assert respuesta.status_code == 422


async def test_quien_se_dio_de_alta_con_mas_de_72_bytes_sigue_entrando(client: AsyncClient, empresa: dict) -> None:
    """El caso de 5a de punta a punta: un usuario con el hash truncado que
    guardó passlib entra por el login con su contraseña entera."""
    sesion = await crear_sesion_en_bd(empresa["id"], "usuario")
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Usuario).where(Usuario.id == sesion.id).values(password_hash=HASH_PASSLIB_LARGA_MULTIBYTE)
        )
        await db.commit()

    respuesta = await client.post("/auth/login", json={"email": sesion.email, "password": PASSWORD_LARGA_MULTIBYTE})
    assert respuesta.status_code == 200, respuesta.text


async def test_un_login_de_mas_de_72_bytes_con_password_mala_da_401(client: AsyncClient, gestor: Sesion) -> None:
    """Ni 422 (el login no valida reglas de contraseña) ni 500 (bcrypt 5 lanza
    con más de 72 bytes si no se corta antes)."""
    respuesta = await client.post("/auth/login", json={"email": gestor.email, "password": "ç" * 40})
    assert respuesta.status_code == 401


def test_la_cli_rechaza_mas_de_72_bytes_antes_de_tocar_la_base_de_datos() -> None:
    argumentos = ["crear-admin", "--empresa", "E", "--nif", "B00000000", "--email", "a@test.subvfy.example.com"]
    argumentos += ["--nombre", "A", "--apellidos", "B", "--password", PASSWORD_LARGA_MULTIBYTE]
    with pytest.raises(SystemExit, match="72 bytes"):
        cli.main(argumentos)
