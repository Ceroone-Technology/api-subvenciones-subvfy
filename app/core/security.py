"""Hashing de contraseñas (bcrypt) y emisión/lectura de JWT (PyJWT).

Estrategia de sesión: **stateless**. No hay tabla de sesiones ni de tokens
revocados — el esquema de `schema-subvfy.sql` no la tiene y no se añade a
propósito, porque una denylist obligaría a consultar la base de datos en
cada petición autenticada. Consecuencias que hay que asumir:

- El logout es del lado del cliente: descarta el token y ya. El servidor no
  puede invalidar un token ya emitido.
- Un token robado sigue siendo válido hasta que caduca. De ahí que el
  access token dure poco (60 min por defecto) y el refresh sea el único
  que dura semanas.
- Lo que sí es inmediato es el bloqueo de un usuario: el token solo lleva
  el id, y la dependencia de autenticación relee usuario y rol de la base
  de datos en cada petición. Cambiar el rol o poner el usuario en
  `inactivo`/`bloqueado` surte efecto en la siguiente llamada, sin esperar
  a que caduque nada.
"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import settings

# Longitud máxima que acepta bcrypt, en bytes UTF-8 (no en caracteres: una ñ
# ocupa dos). Se expone para que los schemas y la CLI la usen en vez de
# repetir el número.
PASSWORD_MIN_LONGITUD = 8
PASSWORD_MAX_BYTES = 72

# Las mismas rondas y el mismo prefijo ($2b$) que usaba passlib: los hashes
# nuevos y los ya guardados tienen la misma forma.
BCRYPT_RONDAS = 12

TIPO_ACCESS = "access"
TIPO_REFRESH = "refresh"


class TokenInvalido(Exception):
    """El token no se puede usar: firma incorrecta, caducado, malformado o
    del tipo equivocado (un refresh donde se esperaba un access)."""


def hashear_password(password: str) -> str:
    """No corta: con más de PASSWORD_MAX_BYTES bytes, bcrypt lanza ValueError.
    Los schemas y la CLI lo rechazan antes, con un mensaje útil."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_RONDAS)).decode("ascii")


def verificar_password(password: str, password_hash: str) -> bool:
    """Corta a PASSWORD_MAX_BYTES bytes antes de comprobar, como hacía passlib.

    Hasta el cambio de librería, el schema contaba caracteres y passlib truncaba
    en silencio, así que quien se dio de alta con más de 72 bytes (una
    contraseña larga con tildes o eñes) tiene guardado el hash de esos 72. Sin
    este corte no podría volver a entrar, y bcrypt 5 lanzaría ValueError: un 500
    en el login para cualquiera que mande una contraseña así.
    """
    return bcrypt.checkpw(password.encode("utf-8")[:PASSWORD_MAX_BYTES], password_hash.encode("ascii"))


def _crear_token(usuario_id: int, tipo: str, duracion: timedelta) -> str:
    ahora = datetime.now(UTC)
    payload = {
        "sub": str(usuario_id),  # el estándar JWT exige que `sub` sea string
        "tipo": tipo,
        "iat": ahora,
        "exp": ahora + duracion,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def crear_access_token(usuario_id: int) -> str:
    return _crear_token(
        usuario_id, TIPO_ACCESS, timedelta(minutes=settings.access_token_expire_minutes)
    )


def crear_refresh_token(usuario_id: int) -> str:
    return _crear_token(
        usuario_id, TIPO_REFRESH, timedelta(days=settings.refresh_token_expire_days)
    )


def leer_token(token: str, tipo_esperado: str) -> int:
    """Valida firma, caducidad y tipo, y devuelve el id de usuario.

    Comprobar el tipo es lo que impide usar un refresh token (de semanas)
    como si fuera un access token en los endpoints normales.
    """
    try:
        # Lista explícita de algoritmos: sin ella, la cabecera del token
        # elegiría con qué se verifica (alg none, confusión de algoritmos).
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.InvalidTokenError as exc:
        raise TokenInvalido(str(exc)) from exc

    if payload.get("tipo") != tipo_esperado:
        raise TokenInvalido(f"Se esperaba un token de tipo '{tipo_esperado}'.")

    sub = payload.get("sub")
    if sub is None or not str(sub).isdigit():
        raise TokenInvalido("El token no identifica a ningún usuario.")
    return int(sub)
