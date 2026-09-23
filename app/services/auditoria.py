"""Identidad con la que firman los procesos automáticos.

`created_by`/`updated_by` apuntan a `usuario.id` en todo el esquema, pero
las filas que escribe el motor de alertas (y mañana la sincronización de la
BDNS o el análisis IA en batch) no las crea ninguna persona. Para eso está
el usuario de sistema que siembra la migración `b7f3c21a9d40`: no puede
iniciar sesión (nace `bloqueado`, con contraseña aleatoria), solo firma.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Usuario

EMAIL_USUARIO_SISTEMA = "sistema@subvfy.es"

# El id no cambia en la vida de una instalación, así que se resuelve una vez
# por proceso en vez de en cada ejecución de alerta.
_id_cacheado: int | None = None


class UsuarioDeSistemaAusente(Exception):
    """Falta el seed de `b7f3c21a9d40`: la base no está migrada del todo."""


async def id_usuario_sistema(db: AsyncSession) -> int:
    global _id_cacheado
    if _id_cacheado is None:
        encontrado = (
            await db.execute(select(Usuario.id).where(Usuario.email == EMAIL_USUARIO_SISTEMA))
        ).scalar_one_or_none()
        if encontrado is None:
            raise UsuarioDeSistemaAusente(
                f"No existe el usuario de sistema ({EMAIL_USUARIO_SISTEMA}). "
                "¿Falta aplicar las migraciones (alembic upgrade head)?"
            )
        _id_cacheado = encontrado
    return _id_cacheado
