"""Configuración de la aplicación, leída de variables de entorno (.env).

Un único objeto `settings`, importado donde haga falta. Nada de valores
mágicos repartidos por el código: todo lo configurable vive aquí.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Entorno
    environment: str = "development"

    # Base de datos (PostgreSQL vía asyncpg)
    database_url: str = "postgresql+asyncpg://subvfy:subvfy@localhost:5432/subvfy"

    # Autenticación JWT
    jwt_secret_key: str = "changeme-en-produccion"
    jwt_algorithm: str = "HS256"
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

    # CORS — orígenes permitidos, separados por coma
    cors_origins: str = "http://localhost:4200"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Settings cacheado (lru_cache) para no releer/parsear el .env en cada request."""
    return Settings()


settings = get_settings()
