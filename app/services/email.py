"""Envío de correo: la tubería, no el contenido.

El contenido de cada aviso vive en `app.services.plantillas_email`; aquí
solo está *cómo* sale un correo del sistema. Separarlo permite probar las
plantillas sin tocar la red y cambiar de proveedor sin reescribir nada.

Dos implementaciones, elegidas por `EMAIL_BACKEND`:

- `consola` (por defecto): escribe el correo en el log y no envía nada. Es
  el valor por defecto a propósito — ningún entorno manda correo real por
  descuido, hay que pedirlo explícitamente.
- `ses`: Amazon SES, coherente con el despliegue en AWS ya decidido. Exige
  tener el dominio del remitente verificado y la cuenta fuera del sandbox
  de SES (si no, solo se puede escribir a direcciones verificadas).

`enviar` es **síncrono** a propósito: boto3 es bloqueante y no tiene versión
async. Quien llame desde código async lo hace con `asyncio.to_thread` (ver
`app.services.notificaciones`), que es más honesto que fingir una corrutina
sobre una llamada que bloquea igual.
"""

import logging
from typing import Any, Protocol

from app.config import settings

logger = logging.getLogger(__name__)

BACKENDS_EMAIL = ("consola", "ses")


class ErrorDeEnvio(Exception):
    """El proveedor rechazó el correo o no se pudo contactar con él."""


class EnviadorEmail(Protocol):
    def enviar(self, *, destinatario: str, asunto: str, html: str, texto: str) -> None: ...


class EnviadorConsola:
    """No envía: deja constancia en el log. Para desarrollo y tests."""

    def enviar(self, *, destinatario: str, asunto: str, html: str, texto: str) -> None:
        logger.info(
            "[email:consola] para=%s asunto=%s\n%s", destinatario, asunto, texto
        )


class EnviadorSES:
    """Amazon SES vía boto3.

    El cliente se crea una sola vez y se reutiliza: construirlo es caro
    (resuelve credenciales y carga el modelo del servicio) y en Lambda se
    aprovecha entre invocaciones del mismo contenedor.
    """

    def __init__(self) -> None:
        # Any: boto3 no publica stubs y sus clientes se construyen en tiempo
        # de ejecución a partir del modelo del servicio, así que no hay tipo
        # que anotar sin añadir boto3-stubs.
        self._cliente: Any = None

    def _obtener_cliente(self) -> Any:
        if self._cliente is None:
            import boto3

            self._cliente = boto3.client("ses", region_name=settings.aws_region)
        return self._cliente

    def enviar(self, *, destinatario: str, asunto: str, html: str, texto: str) -> None:
        remitente = f"{settings.email_remitente_nombre} <{settings.email_remitente}>"
        try:
            self._obtener_cliente().send_email(
                Source=remitente,
                Destination={"ToAddresses": [destinatario]},
                Message={
                    "Subject": {"Data": asunto, "Charset": "UTF-8"},
                    "Body": {
                        # Se manda también texto plano: no es decorativo, los
                        # clientes que bloquean HTML muestran esta parte y
                        # ayuda a no caer en spam.
                        "Text": {"Data": texto, "Charset": "UTF-8"},
                        "Html": {"Data": html, "Charset": "UTF-8"},
                    },
                },
            )
        except Exception as exc:  # boto3 lanza ClientError y varias de botocore
            raise ErrorDeEnvio(f"SES rechazó el envío a {destinatario}: {exc}") from exc


def obtener_enviador() -> EnviadorEmail:
    if settings.email_backend == "ses":
        return EnviadorSES()
    if settings.email_backend != "consola":
        logger.warning(
            "EMAIL_BACKEND=%r no reconocido (validos: %s). Se usa 'consola' y no se envia nada.",
            settings.email_backend,
            ", ".join(BACKENDS_EMAIL),
        )
    return EnviadorConsola()
