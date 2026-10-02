"""Configuración de la aplicación, leída de variables de entorno (.env).

Un único objeto `settings`, importado donde haga falta. Nada de valores
mágicos repartidos por el código: todo lo configurable vive aquí.
"""

from decimal import Decimal
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

    # IA (Análisis + Asistente) — Anthropic. Sin clave, el cliente lo dice
    # antes de llamar a nada (`IANoConfigurada`): la app arranca igual.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    # Por intento: con un reintento, la espera máxima es el doble. Ojo en el
    # Hito 7: API Gateway HTTP API corta a los 30 s.
    anthropic_timeout_segundos: float = 60.0
    # Reintentos del propio SDK (429, 5xx, red), con backoff. Pocos a
    # propósito: hay un usuario esperando la respuesta.
    anthropic_max_reintentos: int = 1
    anthropic_max_tokens: int = 2048
    # Precio en USD por millón de tokens, para `analisis_ia.coste_estimado`.
    # Sin valor por defecto: un precio inventado daría un coste falso. Sin
    # configurar, el coste queda en NULL.
    anthropic_precio_entrada_millon: Decimal | None = None
    anthropic_precio_salida_millon: Decimal | None = None

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

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Settings cacheado (lru_cache) para no releer/parsear el .env en cada request."""
    return Settings()


settings = get_settings()
