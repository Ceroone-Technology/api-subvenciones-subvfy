"""Notificación por email y registro de la ejecución (Hito 4, Funcionalidad 3).

El enviador se inyecta siempre: ningún test toca la red ni depende de
`EMAIL_BACKEND`.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria, Usuario
from app.services import plantillas_email
from app.services.auditoria import id_usuario_sistema
from app.services.email import ErrorDeEnvio
from app.services.notificaciones import (
    ESTADO_ENVIADO,
    ESTADO_ERROR,
    ESTADO_SIN_NOVEDADES,
    notificar_convocatorias,
)
from tests.conftest import Sesion, crear_alerta_en_bd, crear_convocatoria_en_bd, crear_sesion_en_bd


@dataclass
class Correo:
    destinatario: str
    asunto: str
    html: str
    texto: str


class EnviadorDePrueba:
    """Captura lo que se habría enviado. `fallo` simula un SES caído."""

    def __init__(self, fallo: Exception | None = None) -> None:
        self.correos: list[Correo] = []
        self.fallo = fallo

    def enviar(self, *, destinatario: str, asunto: str, html: str, texto: str) -> None:
        if self.fallo is not None:
            raise self.fallo
        self.correos.append(Correo(destinatario=destinatario, asunto=asunto, html=html, texto=texto))


async def _notificar(alerta_id: int, convocatoria_ids: list[int], enviador: EnviadorDePrueba):
    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, alerta_id)
        assert alerta is not None
        convocatorias = []
        for convocatoria_id in convocatoria_ids:
            convocatoria = await db.get(Convocatoria, convocatoria_id)
            assert convocatoria is not None
            convocatorias.append(convocatoria)
        return await notificar_convocatorias(db, alerta, convocatorias, enviador=enviador)


# --- Registro de la ejecución --------------------------------------------------


@pytest.mark.asyncio
async def test_sin_convocatorias_no_envia_pero_registra(gestor: Sesion) -> None:
    """Sin novedades no se manda correo, pero la pasada queda registrada: es
    la diferencia entre "no había nada" y "no se ejecutó"."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    enviador = EnviadorDePrueba()

    resultado = await _notificar(alerta_id, [], enviador)

    assert resultado.estado_envio == ESTADO_SIN_NOVEDADES
    assert resultado.convocatorias_notificadas == 0
    assert enviador.correos == []

    async with AsyncSessionLocal() as db:
        ejecucion = await db.get(AlertaEjecucion, resultado.ejecucion_id)
        assert ejecucion is not None
        assert ejecucion.convocatorias_encontradas == 0
        assert ejecucion.detalle_error is None


@pytest.mark.asyncio
async def test_con_convocatorias_envia_y_registra_enviado(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id, nombre="Kit Digital")
    convocatoria = await crear_convocatoria_en_bd(titulo="Kit Digital Segmento III")
    enviador = EnviadorDePrueba()

    resultado = await _notificar(alerta_id, [convocatoria.id], enviador)

    assert resultado.estado_envio == ESTADO_ENVIADO
    assert resultado.hubo_correo
    assert len(enviador.correos) == 1
    correo = enviador.correos[0]
    assert correo.destinatario == gestor.email
    assert "Kit Digital" in correo.asunto
    assert "Kit Digital Segmento III" in correo.html
    assert "Kit Digital Segmento III" in correo.texto


@pytest.mark.asyncio
async def test_registra_que_convocatorias_se_notificaron(gestor: Sesion) -> None:
    """Es lo que impide volver a avisar de lo mismo en la pasada siguiente."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    primera = await crear_convocatoria_en_bd(titulo="Primera")
    segunda = await crear_convocatoria_en_bd(titulo="Segunda")

    resultado = await _notificar(alerta_id, [primera.id, segunda.id], enviador=EnviadorDePrueba())

    async with AsyncSessionLocal() as db:
        notificadas = set(
            await db.scalars(
                select(AlertaEjecucionConvocatoria.convocatoria_id).where(
                    AlertaEjecucionConvocatoria.alerta_ejecucion_id == resultado.ejecucion_id
                )
            )
        )
    assert notificadas == {primera.id, segunda.id}


@pytest.mark.asyncio
async def test_actualiza_ultima_ejecucion_de_la_alerta(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id)
    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, alerta_id)
        assert alerta is not None and alerta.ultima_ejecucion_at is None

    await _notificar(alerta_id, [], EnviadorDePrueba())

    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, alerta_id)
        assert alerta is not None and alerta.ultima_ejecucion_at is not None


@pytest.mark.asyncio
async def test_la_ejecucion_la_firma_el_usuario_de_sistema(gestor: Sesion) -> None:
    """Las filas de un proceso automático no las crea una persona: llevan la
    identidad que sembró la migración b7f3c21a9d40."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    resultado = await _notificar(alerta_id, [], EnviadorDePrueba())

    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        ejecucion = await db.get(AlertaEjecucion, resultado.ejecucion_id)
        assert ejecucion is not None
        assert ejecucion.created_by == sistema_id
        assert ejecucion.created_by != gestor.id


# --- Canal y destinatario ------------------------------------------------------


@pytest.mark.asyncio
async def test_canal_plataforma_no_manda_correo(gestor: Sesion) -> None:
    """Con canal "plataforma" la propia ejecución es el aviso: no hay correo,
    y eso no es un error."""
    alerta_id = await crear_alerta_en_bd(gestor.id, canal_notificacion="plataforma")
    convocatoria = await crear_convocatoria_en_bd()
    enviador = EnviadorDePrueba()

    resultado = await _notificar(alerta_id, [convocatoria.id], enviador)

    assert resultado.estado_envio == ESTADO_ENVIADO
    assert enviador.correos == []


@pytest.mark.asyncio
async def test_canal_ambos_manda_correo(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id, canal_notificacion="ambos")
    convocatoria = await crear_convocatoria_en_bd()
    enviador = EnviadorDePrueba()

    await _notificar(alerta_id, [convocatoria.id], enviador)

    assert len(enviador.correos) == 1


@pytest.mark.asyncio
async def test_no_se_avisa_a_una_cuenta_bloqueada(empresa: dict) -> None:
    sesion = await crear_sesion_en_bd(empresa["id"], "usuario")
    alerta_id = await crear_alerta_en_bd(sesion.id)
    convocatoria = await crear_convocatoria_en_bd()
    async with AsyncSessionLocal() as db:
        usuario = await db.get(Usuario, sesion.id)
        assert usuario is not None
        usuario.estado = "bloqueado"
        await db.commit()

    enviador = EnviadorDePrueba()
    resultado = await _notificar(alerta_id, [convocatoria.id], enviador)

    assert enviador.correos == []
    # Queda como error y no en silencio: si alguien deja de recibir avisos
    # durante semanas, tiene que verse en el historial de la alerta.
    assert resultado.estado_envio == ESTADO_ERROR
    assert resultado.detalle_error is not None and "bloqueado" in resultado.detalle_error


# --- Fallos del proveedor ------------------------------------------------------


@pytest.mark.asyncio
async def test_un_fallo_de_ses_no_propaga_y_queda_registrado(gestor: Sesion) -> None:
    """Un proveedor caído no puede tumbar la pasada de alertas ni borrar el
    rastro de que la alerta se evaluó."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    convocatoria = await crear_convocatoria_en_bd()
    enviador = EnviadorDePrueba(fallo=ErrorDeEnvio("SES no disponible"))

    resultado = await _notificar(alerta_id, [convocatoria.id], enviador)

    assert resultado.estado_envio == ESTADO_ERROR
    assert resultado.detalle_error is not None
    assert "SES no disponible" in resultado.detalle_error

    async with AsyncSessionLocal() as db:
        ejecucion = await db.get(AlertaEjecucion, resultado.ejecucion_id)
        assert ejecucion is not None
        assert ejecucion.estado_envio == ESTADO_ERROR
        assert ejecucion.convocatorias_encontradas == 1
        # Aunque el correo fallara, la convocatoria queda marcada como
        # tratada: el reintento lo decide el motor, no este servicio.
        enlazadas = await db.scalar(
            select(func.count())
            .select_from(AlertaEjecucionConvocatoria)
            .where(AlertaEjecucionConvocatoria.alerta_ejecucion_id == ejecucion.id)
        )
        assert enlazadas == 1


# --- Contenido del correo ------------------------------------------------------


def _alerta_suelta(**extra) -> Alerta:
    """Instancia sin sesión: las plantillas son funciones puras."""
    return Alerta(nombre=extra.pop("nombre", "Mi alerta"), frecuencia=extra.pop("frecuencia", "diaria"), **extra)


def test_asunto_en_singular_y_en_plural() -> None:
    alerta = _alerta_suelta(nombre="Kit Digital")
    assert plantillas_email.asunto(alerta, 1) == "Subvfy · 1 nueva convocatoria para «Kit Digital»"
    assert plantillas_email.asunto(alerta, 4) == "Subvfy · 4 nuevas convocatorias para «Kit Digital»"


def test_el_enlace_apunta_a_la_ficha_en_subvfy() -> None:
    url = plantillas_email.url_convocatoria("770001")
    assert url.startswith(settings.frontend_base_url)
    assert url.endswith("/convocatorias/770001")


def test_el_html_escapa_el_titulo() -> None:
    """Los títulos vienen de la BDNS, que es una fuente externa: sin escapar,
    un `&` rompe el HTML y una etiqueta se inyectaría en el correo."""
    convocatoria = Convocatoria(codigo_bdns="770002", titulo='Ayudas I+D & <script>alert("x")</script>')
    html = plantillas_email.cuerpo_html(_alerta_suelta(), Usuario(nombre="Ana"), [convocatoria])

    assert "<script>" not in html
    assert "&amp;" in html
    assert "&lt;script&gt;" in html


def test_el_texto_plano_lleva_titulo_codigo_y_enlace() -> None:
    convocatoria = Convocatoria(codigo_bdns="770003", titulo="Ayudas a la exportación")
    texto = plantillas_email.cuerpo_texto(_alerta_suelta(nombre="Exporta"), Usuario(nombre="Ana"), [convocatoria])

    assert "Ayudas a la exportación" in texto
    assert plantillas_email.url_convocatoria("770003") in texto
    assert "Exporta" in texto  # el pie explica por qué se recibe el aviso
    assert "<" not in texto  # es texto plano de verdad, no HTML recortado


def test_se_corta_el_listado_y_se_resume_el_resto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un digest semanal puede traer decenas: el correo dejaría de leerse."""
    monkeypatch.setattr(settings, "email_max_convocatorias", 2)
    convocatorias = [
        Convocatoria(codigo_bdns=f"7700{i}", titulo=f"Convocatoria {i}") for i in range(5)
    ]

    html = plantillas_email.cuerpo_html(_alerta_suelta(), Usuario(nombre="Ana"), convocatorias)
    texto = plantillas_email.cuerpo_texto(_alerta_suelta(), Usuario(nombre="Ana"), convocatorias)

    assert "Convocatoria 1" in html
    assert "Convocatoria 4" not in html
    assert "3" in html  # "y 3 más"
    assert "Y 3 más" in texto


def test_la_descripcion_omite_los_campos_que_faltan() -> None:
    """La ficha de la BDNS llega incompleta a menudo: no se pintan separadores
    sueltos ni etiquetas vacías."""
    convocatoria = Convocatoria(codigo_bdns="770004", titulo="Mínima", financiada_mrr=False)
    html = plantillas_email.cuerpo_html(_alerta_suelta(), Usuario(nombre="Ana"), [convocatoria])

    assert "Mínima" in html
    assert " · ·" not in html
    assert "MRR" not in html
