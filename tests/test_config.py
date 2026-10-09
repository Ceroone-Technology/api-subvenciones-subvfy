"""Arranque seguro de la configuración (AUD-003, AUD-012).

Cada test construye su propio `Settings` en vez de usar el `settings` global:
el global ya se creó al importar la app, con el entorno del contenedor o del
CI. `_env_file=None` evita que cuele el `.env` local, y `monkeypatch` quita
las variables que docker compose (`env_file`) o el workflow inyectan en el
proceso, para que lo que se pruebe sea lo que dice cada test y nada más.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

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


def test_un_secreto_de_menos_de_32_caracteres_no_arranca() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        construir(environment="development", jwt_secret_key="x" * 31)


def test_sin_environment_se_aplican_las_reglas_de_produccion() -> None:
    """Olvidar ENVIRONMENT no puede dejar la configuración en modo permisivo."""
    assert construir(jwt_secret_key=SECRETO_VALIDO).environment == "production"
