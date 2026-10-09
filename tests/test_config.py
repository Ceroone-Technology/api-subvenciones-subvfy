"""Arranque seguro de la configuración (AUD-003, AUD-012).

Cada test construye su propio `Settings` en vez de usar el `settings` global:
el global ya se creó al importar la app, con el entorno del contenedor o del
CI. `_env_file=None` evita que cuele el `.env` local, y `monkeypatch` quita
las variables que docker compose (`env_file`) o el workflow inyectan en el
proceso, para que lo que se pruebe sea lo que dice cada test y nada más.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    JWT_SECRET_KEY_CI,
    JWT_SECRET_KEY_EJEMPLO,
    JWT_SECRET_KEY_MIN_CARACTERES,
    Settings,
)

VARIABLES_DEL_ENTORNO = ("ENVIRONMENT", "JWT_SECRET_KEY", "JWT_ALGORITHM", "DATABASE_ECHO")
SECRETO_VALIDO = "un-secreto-de-test-con-mas-de-32-caracteres"


@pytest.fixture(autouse=True)
def entorno_limpio(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in VARIABLES_DEL_ENTORNO:
        monkeypatch.delenv(variable, raising=False)


def construir(**valores: object) -> Settings:
    return Settings(_env_file=None, **valores)  # type: ignore[call-arg]


def test_sin_jwt_secret_key_no_arranca() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        construir(environment="development")


def test_el_error_dice_como_generar_el_secreto() -> None:
    with pytest.raises(ValidationError, match="secrets.token_urlsafe"):
        construir(environment="development")


def test_un_secreto_vacio_no_arranca() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        construir(environment="development", jwt_secret_key="")


def test_un_secreto_de_menos_de_32_caracteres_no_arranca() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        construir(environment="development", jwt_secret_key="x" * 31)


def test_el_error_no_vuelca_los_valores_leidos() -> None:
    """El error de arranque acaba en el log: no puede llevar el secreto ni la
    contraseña de la base de datos."""
    secreto_corto = "secreto-corto-que-no-debe-salir"
    url = "postgresql+asyncpg://u:password-que-no-debe-salir@h/db"
    with pytest.raises(ValidationError) as error:
        construir(environment="development", jwt_secret_key=secreto_corto, database_url=url)
    assert secreto_corto not in str(error.value)
    assert "password-que-no-debe-salir" not in str(error.value)


def test_un_secreto_de_32_caracteres_arranca() -> None:
    secreto = "x" * JWT_SECRET_KEY_MIN_CARACTERES
    assert construir(environment="production", jwt_secret_key=secreto).jwt_secret_key == secreto


def test_la_variable_de_entorno_tambien_se_valida(monkeypatch: pytest.MonkeyPatch) -> None:
    """El camino real: el secreto llega por el entorno, no por argumento."""
    monkeypatch.setenv("JWT_SECRET_KEY", "corto")
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        construir(environment="development")


def test_sin_environment_se_aplican_las_reglas_de_produccion() -> None:
    """Olvidar ENVIRONMENT no puede dejar la configuración en modo permisivo."""
    assert construir(jwt_secret_key=SECRETO_VALIDO).environment == "production"


def test_un_environment_desconocido_no_arranca() -> None:
    """Una errata como `prod` no puede tratarse en silencio como otro entorno."""
    with pytest.raises(ValidationError, match="environment"):
        construir(environment="prod", jwt_secret_key=SECRETO_VALIDO)


@pytest.mark.parametrize("secreto_publico", [JWT_SECRET_KEY_EJEMPLO, JWT_SECRET_KEY_CI])
def test_la_app_no_arranca_con_un_secreto_publico_fuera_de_development(secreto_publico: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        construir(environment="production", jwt_secret_key=secreto_publico)


def test_sin_environment_tampoco_arranca_con_el_secreto_de_ejemplo() -> None:
    """El caso del despliegue que copia .env.example y olvida ENVIRONMENT."""
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        construir(jwt_secret_key=JWT_SECRET_KEY_EJEMPLO)


@pytest.mark.parametrize("secreto_publico", [JWT_SECRET_KEY_EJEMPLO, JWT_SECRET_KEY_CI])
def test_los_secretos_publicos_valen_en_development(secreto_publico: str) -> None:
    assert construir(environment="development", jwt_secret_key=secreto_publico).environment == "development"


def test_otro_algoritmo_jwt_no_arranca() -> None:
    with pytest.raises(ValidationError, match="jwt_algorithm"):
        construir(environment="development", jwt_secret_key=SECRETO_VALIDO, jwt_algorithm="none")


@pytest.mark.parametrize(
    ("archivo", "linea"),
    [
        (".env.example", f"JWT_SECRET_KEY={JWT_SECRET_KEY_EJEMPLO}"),
        (".github/workflows/ci.yml", f"JWT_SECRET_KEY: {JWT_SECRET_KEY_CI}"),
    ],
)
def test_los_valores_publicos_son_los_del_repositorio(archivo: str, linea: str) -> None:
    """Si alguien cambia el valor de .env.example o del CI sin tocar las
    constantes, el rechazo fuera de development dejaría de cubrirlo.

    Dentro del contenedor de desarrollo se salta: .dockerignore deja fuera
    .env.* y .github. Donde corre de verdad es en el CI, con el repo entero.
    """
    ruta = Path(__file__).resolve().parent.parent / archivo
    if not ruta.exists():
        pytest.skip(f"{archivo} no está en esta copia (imagen de desarrollo)")
    lineas = [linea_leida.strip() for linea_leida in ruta.read_text(encoding="utf-8").splitlines()]
    assert linea in lineas
