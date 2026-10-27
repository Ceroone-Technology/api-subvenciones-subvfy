"""Identidad de los procesos automáticos.

Los procesos que escriben sin que haya una persona detrás (motor de alertas,
sincronización de la BDNS, análisis IA en batch) firman
`created_by`/`updated_by` con el usuario de sistema que siembra la migración
`b7f3c21a9d40`. **No es una cuenta de acceso**: nace `bloqueado` y con
contraseña aleatoria, y aquí solo se resuelve su id.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Usuario

EMAIL_SISTEMA = "sistema@subvfy.es"

# El id no cambia en la vida de la base de datos, así que se resuelve una vez
# por proceso: el motor de alertas lo necesita en cada ciclo.
_id_cacheado: int | None = None


class UsuarioSistemaAusente(RuntimeError):
    """Falta el seed de `b7f3c21a9d40`: la base de datos no está migrada."""


async def id_usuario_sistema(db: AsyncSession) -> int:
    global _id_cacheado
    if _id_cacheado is None:
        _id_cacheado = await db.scalar(select(Usuario.id).where(Usuario.email == EMAIL_SISTEMA))
        if _id_cacheado is None:
            raise UsuarioSistemaAusente(
                f"No existe el usuario de sistema ({EMAIL_SISTEMA}): ejecuta 'alembic upgrade head'."
            )
    return _id_cacheado
