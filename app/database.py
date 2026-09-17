"""Conexión async a PostgreSQL con SQLAlchemy 2.0.

`Base` es la clase declarativa de la que heredarán los modelos (Hito 2,
Funcionalidad 2). `get_db` es la dependencia de FastAPI que entrega una
sesión por request y la cierra sola al terminar.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import settings

# NullPool: sin pool de conexiones persistente. Es la recomendación oficial
# de SQLAlchemy para entornos donde el event loop puede cambiar entre usos
# del engine (exactamente el caso de AWS Lambda, nuestro destino de
# despliegue — cada invocación puede correr en un loop distinto — y el de
# los tests con pytest-asyncio). El coste es abrir una conexión nueva por
# request en vez de reusar una del pool; a este volumen no es relevante.
engine = create_async_engine(
    settings.database_url, echo=settings.environment == "development", poolclass=NullPool
)

AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    """Clase base declarativa. Todos los modelos de app/models/ heredan de aquí.

    `type_annotation_map` hace que `Mapped[int]` se traduzca a BIGINT (no
    INTEGER) en todos los modelos, para que las PK/FK generadas coincidan
    con los `bigserial`/`bigint` de schema-subvfy.sql sin repetir
    `BigInteger` en cada columna.
    """

    type_annotation_map = {int: BigInteger}


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
