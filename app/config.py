"""Configuración de la aplicación, leída de variables de entorno (.env).

Un único objeto `settings`, importado donde haga falta. Nada de valores
mágicos repartidos por el código: todo lo configurable vive aquí.
"""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENTORNOS = ("development", "production")

# Secretos de firma que están escritos en el repositorio y por tanto son
# públicos: valen para desarrollo y para el CI, nunca fuera de development.
JWT_SECRET_KEY_EJEMPLO = "dev-only-inseguro-no-usar-fuera-de-desarrollo-0000"
JWT_SECRET_KEY_CI = "ci-only-inseguro-no-usar-fuera-del-ci-000000000000"
JWT_SECRETOS_PUBLICOS = frozenset({JWT_SECRET_KEY_EJEMPLO, JWT_SECRET_KEY_CI})
JWT_SECRET_KEY_MIN_CARACTERES = 32

_COMO_GENERAR_SECRETO = 'genera uno con: python -c "import secrets; print(secrets.token_urlsafe(48))"'


class Settings(BaseSettings):
    # hide_input_in_errors: un error de validación imprime por defecto todo lo
    # leído (secreto y DATABASE_URL con contraseña incluidos) en el log de arranque.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    # Entorno. Por defecto el estricto: un despliegue que olvide ENVIRONMENT
    # tiene que fallar en cerrado, no quedarse en el modo permisivo.
    environment: Literal[ENTORNOS] = "production"  # type: ignore[valid-type]

    # Base de datos (PostgreSQL vía asyncpg)
    database_url: str = "postgresql+asyncpg://subvfy:subvfy@localhost:5432/subvfy"
    # Echo de SQL de SQLAlchemy. Vuelca cada sentencia CON SUS PARÁMETROS
    # (hashes de contraseña, emails, tokens): solo se enciende a mano para
    # depurar y nunca depende del entorno (AUD-012).
    database_echo: bool = False

    # Autenticación JWT. Obligatorio y sin valor por defecto (AUD-003): con un
    # secreto conocido cualquiera puede firmar tokens de cualquier usuario.
    jwt_secret_key: str = Field(min_length=JWT_SECRET_KEY_MIN_CARACTERES)
    # Solo HS256: es el que se usa, y aceptar otro valor de la configuración
    # abriría la puerta a algoritmos que nadie ha revisado.
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = 60
    # El refresh token es el que sostiene la sesión larga del frontend.
    # Al ser stateless no se puede revocar, así que su duración es el
    # límite real de exposición si se roba: no lo subas sin pensarlo.
    refresh_token_expire_days: int = 30

    # IA (Análisis + Asistente) — Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # Notificaciones por email (Hito 4, Funcionalidad 3)
    # "consola" solo escribe el correo en el log: es el valor por defecto a
    # propósito, para que ningún entorno mande correo real sin pedirlo.
    email_backend: str = "consola"
    email_remitente: str = "alertas@subvfy.es"
    email_remitente_nombre: str = "Subvfy"
    # Cuántas convocatorias se listan en el correo antes de cortar con un
    # "y otras N más": un digest semanal puede traer decenas.
    email_max_convocatorias: int = 10
    aws_region: str = "eu-west-1"

    # Enlaces del correo hacia el frontend Angular. La ruta es una plantilla
    # con {codigo_bdns}; ajustar al routing real de la app.
    frontend_base_url: str = "http://localhost:4200"
    frontend_ruta_convocatoria: str = "/convocatorias/{codigo_bdns}"
    frontend_ruta_alertas: str = "/alertas"

    # BDNS (fuente de verdad de las convocatorias). Solo lectura, sin clave:
    # la API es pública. Sin rate limit documentado, de ahí los topes.
    bdns_base_url: str = "https://www.infosubvenciones.es/bdnstrans/api"
    bdns_timeout_segundos: float = 10.0
    bdns_tamano_pagina: int = 100
    bdns_max_paginas: int = 20
    # Ventana de la primera evaluación de una alerta, cuando todavía no tiene
    # ultima_ejecucion_at: sin esto habría que decidir entre traer la BDNS
    # entera o nada.
    bdns_dias_primera_ejecucion: int = 7

    # Nivel de log de la aplicación. Sin esto, los INFO de app.* no se ven:
    # uvicorn solo configura sus propios loggers.
    log_level: str = "INFO"

    # Motor de alertas (APScheduler en local; en Lambda lo dispara EventBridge)
    # Desactivado por defecto: en tests y en la Lambda del Hito 7 nada debe
    # arrancar por su cuenta. Se activa en el .env de desarrollo.
    scheduler_habilitado: bool = False
    scheduler_intervalo_minutos: int = 15

    # Espera entre reintentos de una alerta que falla: base × 2^(fallos-1),
    # con tope. Sin esto, una alerta rota se reintentaría en cada ciclo.
    alertas_reintento_base_minutos: int = 15
    alertas_reintento_max_horas: int = 24

    # CORS — orígenes permitidos, separados por coma
    cors_origins: str = "http://localhost:4200"

    @model_validator(mode="before")
    @classmethod
    def _exigir_jwt_secret_key(cls, datos: Any) -> Any:
        """Mensaje propio cuando falta o es corto: el "Field required" de
        Pydantic nombra el campo en minúsculas y no dice qué hacer."""
        if isinstance(datos, dict):
            secreto = datos.get("jwt_secret_key")
            if not secreto:
                raise ValueError(f"Falta JWT_SECRET_KEY: es obligatoria; {_COMO_GENERAR_SECRETO}")
            # La longitud se mide sin los espacios de los extremos: 40 espacios
            # no son un secreto de 40 caracteres.
            if isinstance(secreto, str) and len(secreto.strip()) < JWT_SECRET_KEY_MIN_CARACTERES:
                raise ValueError(
                    f"JWT_SECRET_KEY debe tener al menos {JWT_SECRET_KEY_MIN_CARACTERES} caracteres; "
                    f"{_COMO_GENERAR_SECRETO}"
                )
        return datos

    @model_validator(mode="after")
    def _rechazar_secretos_publicos_fuera_de_development(self) -> "Settings":
        # strip: un espacio pegado al valor de .env.example no lo convierte en otro secreto.
        if self.environment != "development" and self.jwt_secret_key.strip() in JWT_SECRETOS_PUBLICOS:
            raise ValueError(
                f"JWT_SECRET_KEY usa un valor público del repositorio (.env.example o CI) con "
                f"ENVIRONMENT={self.environment}; {_COMO_GENERAR_SECRETO}"
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Settings cacheado (lru_cache) para no releer/parsear el .env en cada request."""
    return Settings()


settings = get_settings()
