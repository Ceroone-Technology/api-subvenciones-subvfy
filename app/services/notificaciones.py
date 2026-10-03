"""Envío del aviso de una alerta y cierre de su ejecución (Hito 4, F3).

**Este servicio no crea nada.** Quien registra la ejecución y decide qué
convocatorias son novedad es el motor (`registrar_ejecucion`, en
`app.services.deduplicacion_alertas`), que la deja en `pendiente_envio`
cuando hay algo que contar. Aquí se recoge ese testigo: se manda el correo y
se cierra la ejecución en `enviado`.

Hacerlo así, y no registrando por nuestra cuenta, es lo que mantiene una
sola fuente de verdad: la deduplicación entre ejecuciones, el `UNIQUE` que
la respalda y el recorte a las longitudes de columna viven en el motor y no
se duplican aquí.

**`pendiente_envio` es la cola.** Una ejecución en ese estado es un aviso que
debe salir; cualquier otro estado ya está resuelto y esta función lo ignora.
Eso la hace segura de reintentar: llamarla dos veces sobre una ejecución ya
enviada no manda un segundo correo.

**Un fallo de envío no cierra la ejecución: la deja en la cola.** No se
propaga la excepción (un SES caído tumbaría la pasada entera de alertas),
pero tampoco se marca `error`: la ejecución se queda en `pendiente_envio`
con el motivo apuntado en `detalle_error`, así que el aviso sigue debiendo
salir y un reintento puede recogerlo.

Esto es deliberado y vale la pena entenderlo, porque **`error` no es nuestro**.
Ese estado significa una sola cosa: la ejecución no llegó a término (la BDNS
no respondió, la base de datos rechazó el lote). Lo escribe el motor. Si
además lo usáramos para "se ejecutó bien pero el correo no salió", la misma
columna tendría dos significados opuestos: en el primero las convocatorias no
se registraron y volverán a encontrarse, y en el segundo sí se registraron y
no se volverán a anunciar nunca.

Cómo se lee entonces una ejecución en `pendiente_envio`:

- `detalle_error` nulo — todavía no se ha intentado enviar.
- `detalle_error` con texto — se intentó y falló; ahí está el motivo y
  `updated_at` dice cuándo fue el último intento.

**Hoy nadie barre esa cola.** El ciclo del motor llama a `enviar_aviso` con la
ejecución que acaba de crear, no con las pendientes de pasadas anteriores, así
que un aviso fallido queda visible en el historial pero no se reenvía solo. El
barrido es trabajo aparte, y este módulo ya deja el dato preparado para él.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria, Usuario
from app.services import plantillas_email
from app.services.email import EnviadorEmail, obtener_enviador
from app.services.sistema import id_usuario_sistema

logger = logging.getLogger(__name__)

ESTADO_PENDIENTE = "pendiente_envio"
ESTADO_ENVIADO = "enviado"
ESTADO_SIN_NOVEDADES = "sin_novedades"
# `error` existe en el CHECK de la tabla, pero no se escribe desde aquí: es del
# motor, para las ejecuciones que no llegaron a término. Ver la cabecera.

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


async def enviar_aviso(
    db: AsyncSession,
    ejecucion: AlertaEjecucion,
    *,
    enviador: EnviadorEmail | None = None,
) -> ResultadoNotificacion:
    """Manda el aviso de una ejecución pendiente y, si sale, la cierra.

    Si no sale, la ejecución **sigue en `pendiente_envio`** con el motivo en
    `detalle_error`: el aviso continúa debiendo salir. Ver la cabecera del
    módulo sobre por qué no se marca `error`.

    `enviador` se inyecta en los tests; en producción se resuelve desde
    `EMAIL_BACKEND`.
    """
    if ejecucion.estado_envio != ESTADO_PENDIENTE:
        # Nada que hacer: o no hubo novedades, o este aviso ya salió.
        return ResultadoNotificacion(
            ejecucion_id=ejecucion.id,
            estado_envio=ejecucion.estado_envio,
            convocatorias_notificadas=ejecucion.convocatorias_encontradas,
            detalle_error=ejecucion.detalle_error,
        )

    detalle_error = await _intentar_aviso(db, ejecucion, enviador)

    if detalle_error is None:
        ejecucion.estado_envio = ESTADO_ENVIADO
    # En el camino bueno esto limpia el detalle de un intento anterior fallido.
    ejecucion.detalle_error = detalle_error
    ejecucion.updated_by = await id_usuario_sistema(db)
    await db.commit()

    return ResultadoNotificacion(
        ejecucion_id=ejecucion.id,
        estado_envio=ejecucion.estado_envio,
        convocatorias_notificadas=ejecucion.convocatorias_encontradas,
        detalle_error=detalle_error,
    )


async def _intentar_aviso(
    db: AsyncSession, ejecucion: AlertaEjecucion, enviador: EnviadorEmail | None
) -> str | None:
    """Devuelve el detalle del error, o None si el aviso quedó resuelto bien."""
    alerta = await db.get(Alerta, ejecucion.alerta_id)
    if alerta is None:  # pragma: no cover - la FK lo impide
        return f"La ejecución {ejecucion.id} no tiene alerta asociada."

    if alerta.canal_notificacion not in CANALES_CON_EMAIL:
        # Canal "plataforma": la propia ejecución, con sus convocatorias
        # enlazadas, *es* la notificación. No hay nada que enviar y no es un
        # error, así que cuenta como entregada.
        return None

    usuario = await db.get(Usuario, alerta.usuario_id)
    if usuario is None:  # pragma: no cover - la FK lo impide
        return f"La alerta {alerta.id} no tiene usuario asociado."
    if usuario.estado != "activo":
        # Se registra como error, no en silencio: si a alguien se le avisa de
        # nada durante semanas por estar bloqueado, tiene que verse en el
        # historial de la alerta.
        return f"No se avisa a {usuario.email}: la cuenta está en estado '{usuario.estado}'."

    convocatorias = await convocatorias_de(db, ejecucion.id)
    if not convocatorias:  # pragma: no cover - pendiente_envio implica que hay
        return f"La ejecución {ejecucion.id} está pendiente de envío pero no tiene convocatorias."

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
        logger.exception("Fallo al notificar la ejecución %s de la alerta %s", ejecucion.id, alerta.id)
        return f"{type(exc).__name__}: {exc}"
    return None


async def convocatorias_de(db: AsyncSession, ejecucion_id: int) -> Sequence[Convocatoria]:
    """Las convocatorias que registró esa ejecución, en el orden en que se
    registraron."""
    filas = await db.scalars(
        select(Convocatoria)
        .join(
            AlertaEjecucionConvocatoria,
            AlertaEjecucionConvocatoria.convocatoria_id == Convocatoria.id,
        )
        .where(AlertaEjecucionConvocatoria.alerta_ejecucion_id == ejecucion_id)
        .order_by(AlertaEjecucionConvocatoria.id)
    )
    return list(filas)
