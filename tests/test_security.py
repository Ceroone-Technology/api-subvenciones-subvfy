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

from app.config import settings
from app.core.security import (
    TIPO_ACCESS,
    TIPO_REFRESH,
    TokenInvalido,
    crear_access_token,
    crear_refresh_token,
    leer_token,
    verificar_password,
)
from tests.conftest import Sesion

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
