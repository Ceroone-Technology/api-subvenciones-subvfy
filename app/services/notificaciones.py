"""Notificación de una alerta y registro de su ejecución (Hito 4, F3).

Es el punto donde se juntan las dos tareas de la funcionalidad: se manda el
correo **y** se deja constancia de cómo fue en `alerta_ejecucion`. Van
juntas porque el estado del envío no se puede registrar en otro sitio sin
duplicar la lógica de qué se considera enviado.

Quien llama es el motor de ejecución (Hito 4, Funcionalidad 2, todavía por
construir): decide qué alertas tocan y qué convocatorias son nuevas, y llama
aquí con el resultado. Este servicio no consulta la BDNS ni evalúa criterios.

**La ejecución se registra pase lo que pase.** Si el proveedor de correo
falla, no se propaga la excepción: se guarda `estado_envio = "error"` con el
detalle. Un fallo de SES no puede hacer que se pierda el rastro de que la
alerta se evaluó, ni tumbar el lote entero de alertas de esa pasada.

**Las convocatorias notificadas se persisten** en `alerta_ejecucion_convocatoria`
incluso cuando el envío falla: es lo que impide que el siguiente intento las
trate como nuevas otra vez. Al motor le corresponde decidir si reintenta.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria, Usuario
from app.services import plantillas_email
from app.services.auditoria import id_usuario_sistema
from app.services.email import EnviadorEmail, obtener_enviador

logger = logging.getLogger(__name__)

ESTADO_ENVIADO = "enviado"
ESTADO_SIN_NOVEDADES = "sin_novedades"
ESTADO_ERROR = "error"

# Canales de `alerta.canal_notificacion` que implican mandar un correo.
CANALES_CON_EMAIL = ("email", "ambos")


@dataclass(frozen=True)
class ResultadoNotificacion:
    ejecucion_id: int
    estado_envio: str
    convocatorias_notificadas: int
    detalle_error: str | None = None

    @property
    def hubo_correo(self) -> bool:
        return self.estado_envio == ESTADO_ENVIADO and self.convocatorias_notificadas > 0


async def notificar_convocatorias(
    db: AsyncSession,
    alerta: Alerta,
    convocatorias: Sequence[Convocatoria],
    *,
    enviador: EnviadorEmail | None = None,
) -> ResultadoNotificacion:
    """Notifica las convocatorias nuevas de una alerta y registra la ejecución.

    `enviador` se inyecta en los tests; en producción se resuelve desde
    `EMAIL_BACKEND`.
    """
    autor = await id_usuario_sistema(db)
    ejecucion = AlertaEjecucion(
        alerta_id=alerta.id,
        convocatorias_encontradas=len(convocatorias),
        estado_envio=ESTADO_SIN_NOVEDADES,
        created_by=autor,
        updated_by=autor,
    )
    db.add(ejecucion)
    await db.flush()  # para tener ejecucion.id antes de enlazar convocatorias

    db.add_all(
        AlertaEjecucionConvocatoria(
            alerta_ejecucion_id=ejecucion.id,
            convocatoria_id=convocatoria.id,
            created_by=autor,
            updated_by=autor,
        )
        for convocatoria in convocatorias
    )

    detalle_error: str | None = None
    if convocatorias:
        detalle_error = await _intentar_aviso(db, alerta, convocatorias, enviador)
        ejecucion.estado_envio = ESTADO_ERROR if detalle_error else ESTADO_ENVIADO
        ejecucion.detalle_error = detalle_error

    # Marca la alerta como evaluada aunque no hubiera novedades: es lo que
    # usa el motor para saber desde cuándo buscar en la pasada siguiente.
    alerta.ultima_ejecucion_at = func.now()
    alerta.updated_by = autor
    await db.commit()

    return ResultadoNotificacion(
        ejecucion_id=ejecucion.id,
        estado_envio=ejecucion.estado_envio,
        convocatorias_notificadas=len(convocatorias),
        detalle_error=detalle_error,
    )


async def _intentar_aviso(
    db: AsyncSession,
    alerta: Alerta,
    convocatorias: Sequence[Convocatoria],
    enviador: EnviadorEmail | None,
) -> str | None:
    """Devuelve el detalle del error, o None si el aviso quedó resuelto bien."""
    if alerta.canal_notificacion not in CANALES_CON_EMAIL:
        # Canal "plataforma": la propia ejecución, con sus convocatorias
        # enlazadas, *es* la notificación. No hay nada que enviar y no es un
        # error, así que cuenta como entregada.
        return None

    usuario = await db.get(Usuario, alerta.usuario_id)
    if usuario is None:
        return f"La alerta {alerta.id} no tiene usuario asociado."
    if usuario.estado != "activo":
        # Se registra como error, no en silencio: si a alguien se le avisa de
        # nada durante semanas por estar bloqueado, tiene que verse en el
        # historial de la alerta.
        return f"No se avisa a {usuario.email}: la cuenta está en estado '{usuario.estado}'."

    destino = enviador or obtener_enviador()
    try:
        # boto3 es bloqueante y no tiene versión async: se saca del event
        # loop para no congelar el resto de la pasada de alertas.
        await asyncio.to_thread(
            destino.enviar,
            destinatario=usuario.email,
            asunto=plantillas_email.asunto(alerta, len(convocatorias)),
            html=plantillas_email.cuerpo_html(alerta, usuario, convocatorias),
            texto=plantillas_email.cuerpo_texto(alerta, usuario, convocatorias),
        )
    except Exception as exc:
        # A propósito no se relanza: ver la nota de cabecera del módulo.
        logger.exception("Fallo al notificar la alerta %s", alerta.id)
        return f"{type(exc).__name__}: {exc}"
    return None
