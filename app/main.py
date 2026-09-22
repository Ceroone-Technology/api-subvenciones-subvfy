"""Punto de entrada de la API — app-subvenciones-subvfy.

Arranque local: `uvicorn app.main:app --reload`
Docs interactivas (OpenAPI): http://localhost:8000/docs
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import alertas, auth, empresas, favoritos, roles, usuarios
from app.config import settings

app = FastAPI(
    title="Subvfy API",
    description="API de app-subvenciones-subvfy: favoritos, alertas, autenticación y análisis con IA.",
    version="0.1.0",
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
