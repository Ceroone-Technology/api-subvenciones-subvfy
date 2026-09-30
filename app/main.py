"""Punto de entrada de la API — app-subvenciones-subvfy.

Arranque local: `uvicorn app.main:app --reload`
Docs interactivas (OpenAPI): http://localhost:8000/docs
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import alertas, auth, empresas, favoritos, roles, usuarios
from app.config import settings
from app.core.scheduler import iniciar_scheduler, parar_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranca el motor de alertas al levantar la app y lo para al apagarla.

    Solo si `SCHEDULER_HABILITADO`. Los tests usan httpx + ASGITransport, que
    no dispara el lifespan, así que ahí no arranca nunca.
    """
    # uvicorn solo configura sus loggers, así que sin esto los INFO de la
    # app (motor de alertas incluido) no saldrían por la salida estándar.
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    scheduler = iniciar_scheduler()
    try:
        yield
    finally:
        await parar_scheduler(scheduler)


app = FastAPI(
    title="Subvfy API",
    description="API de app-subvenciones-subvfy: favoritos, alertas, autenticación y análisis con IA.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(roles.router)
app.include_router(empresas.router)
app.include_router(usuarios.router)
app.include_router(favoritos.router)
app.include_router(alertas.router)

# Los routers de negocio restantes (análisis IA)
# se registran aquí a medida que se construyen, uno por Funcionalidad del
# backlog (api-hitos-funcionalidades-tareas-subvfy.docx).
