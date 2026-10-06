"""Envío del aviso y cierre de la ejecución (Hito 4, Funcionalidad 3).

El servicio ya no registra nada: consume las ejecuciones que el motor deja en
`pendiente_envio`. Por eso los tests parten de `registrar_ejecucion`, igual
que en producción, en vez de fabricar filas a mano.

El enviador se inyecta siempre: ningún test toca la red ni depende de
`EMAIL_BACKEND`.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Alerta, AlertaEjecucion, Convocatoria, Usuario
from app.services import plantillas_email
from app.services.bdns_cliente import ConvocatoriaBdns
from app.services.deduplicacion_alertas import registrar_ejecucion
from app.services.email import ErrorDeEnvio
from app.services.notificaciones import (
    ESTADO_ENVIADO,
    ESTADO_PENDIENTE,
    ESTADO_SIN_NOVEDADES,
    enviar_aviso,
)
from app.services.sistema import id_usuario_sistema
from tests.conftest import (
    Sesion,
    codigo_bdns_de_prueba,
    crear_alerta_en_bd,
    crear_sesion_en_bd,
)


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


def _de_la_bdns(titulo: str = "Ayudas a la digitalización", **extra) -> ConvocatoriaBdns:
    """Una convocatoria como la devolvería la BDNS, con lo mínimo."""
    campos = {
        "id_bdns": 0,
        "codigo_bdns": codigo_bdns_de_prueba(),
        "titulo": titulo,
        "fecha_registro": None,
        "nivel1": "ESTADO",
        "nivel2": "Estado",
        "nivel3": "Red.es",
        "financiada_mrr": False,
    }
    campos.update(extra)
    return ConvocatoriaBdns(**campos)


async def _ejecutar(alerta_id: int, convocatorias: list[ConvocatoriaBdns]) -> int:
    """Registra una ejecución como haría el motor y devuelve su id."""
    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, alerta_id)
        assert alerta is not None
        ejecucion = await registrar_ejecucion(
            db, alerta, convocatorias, usuario_sistema_id=await id_usuario_sistema(db)
        )
        return ejecucion.id


async def _avisar(ejecucion_id: int, enviador: EnviadorDePrueba):
    async with AsyncSessionLocal() as db:
        ejecucion = await db.get(AlertaEjecucion, ejecucion_id)
        assert ejecucion is not None
        return await enviar_aviso(db, ejecucion, enviador=enviador)


# --- El testigo entre el motor y el aviso --------------------------------------


@pytest.mark.asyncio
async def test_una_ejecucion_sin_novedades_no_se_avisa(gestor: Sesion) -> None:
    """El motor la deja en `sin_novedades`, que ya es un estado resuelto: no
    hay correo ni cambio de estado."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    ejecucion_id = await _ejecutar(alerta_id, [])
    enviador = EnviadorDePrueba()

    resultado = await _avisar(ejecucion_id, enviador)

    assert resultado.estado_envio == ESTADO_SIN_NOVEDADES
    assert enviador.correos == []


@pytest.mark.asyncio
async def test_una_ejecucion_pendiente_se_avisa_y_se_cierra(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id, nombre="Kit Digital")
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns("Kit Digital Segmento III")])
    enviador = EnviadorDePrueba()

    resultado = await _avisar(ejecucion_id, enviador)

    assert resultado.estado_envio == ESTADO_ENVIADO
    assert resultado.hubo_correo
    assert len(enviador.correos) == 1
    correo = enviador.correos[0]
    assert correo.destinatario == gestor.email
    assert "Kit Digital" in correo.asunto
    assert "Kit Digital Segmento III" in correo.html
    assert "Kit Digital Segmento III" in correo.texto

    # El estado queda persistido, no solo en el resultado.
    async with AsyncSessionLocal() as db:
        ejecucion = await db.get(AlertaEjecucion, ejecucion_id)
        assert ejecucion is not None
        assert ejecucion.estado_envio == ESTADO_ENVIADO
        assert ejecucion.detalle_error is None


@pytest.mark.asyncio
async def test_avisar_dos_veces_no_manda_dos_correos(gestor: Sesion) -> None:
    """Segura de reintentar: una vez cerrada, la ejecución ya no está en la
    cola de `pendiente_envio`."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns()])

    primero = EnviadorDePrueba()
    segundo = EnviadorDePrueba()
    await _avisar(ejecucion_id, primero)
    resultado = await _avisar(ejecucion_id, segundo)

    assert len(primero.correos) == 1
    assert segundo.correos == []
    assert resultado.estado_envio == ESTADO_ENVIADO


@pytest.mark.asyncio
async def test_el_correo_lleva_todas_las_convocatorias_de_la_ejecucion(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id)
    primera = _de_la_bdns("Primera convocatoria")
    segunda = _de_la_bdns("Segunda convocatoria")
    ejecucion_id = await _ejecutar(alerta_id, [primera, segunda])
    enviador = EnviadorDePrueba()

    await _avisar(ejecucion_id, enviador)

    texto = enviador.correos[0].texto
    assert "Primera convocatoria" in texto
    assert "Segunda convocatoria" in texto
    assert plantillas_email.url_convocatoria(primera.codigo_bdns) in texto


@pytest.mark.asyncio
async def test_el_cierre_lo_firma_el_usuario_de_sistema(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id)
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns()])

    await _avisar(ejecucion_id, EnviadorDePrueba())

    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        ejecucion = await db.get(AlertaEjecucion, ejecucion_id)
        assert ejecucion is not None
        assert ejecucion.updated_by == sistema_id
        assert ejecucion.updated_by != gestor.id


# --- Canal y destinatario ------------------------------------------------------


@pytest.mark.asyncio
async def test_canal_plataforma_no_manda_correo(gestor: Sesion) -> None:
    """Con canal "plataforma" la propia ejecución es el aviso: no hay correo,
    y eso no es un error."""
    alerta_id = await crear_alerta_en_bd(gestor.id, canal_notificacion="plataforma")
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns()])
    enviador = EnviadorDePrueba()

    resultado = await _avisar(ejecucion_id, enviador)

    assert resultado.estado_envio == ESTADO_ENVIADO
    assert enviador.correos == []


@pytest.mark.asyncio
async def test_canal_ambos_manda_correo(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id, canal_notificacion="ambos")
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns()])
    enviador = EnviadorDePrueba()

    await _avisar(ejecucion_id, enviador)

    assert len(enviador.correos) == 1


@pytest.mark.asyncio
async def test_no_se_avisa_a_una_cuenta_bloqueada(empresa: dict) -> None:
    sesion = await crear_sesion_en_bd(empresa["id"], "usuario")
    alerta_id = await crear_alerta_en_bd(sesion.id)
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns()])
    async with AsyncSessionLocal() as db:
        usuario = await db.get(Usuario, sesion.id)
        assert usuario is not None
        usuario.estado = "bloqueado"
        await db.commit()

    enviador = EnviadorDePrueba()
    resultado = await _avisar(ejecucion_id, enviador)

    assert enviador.correos == []
    # Sigue en la cola, no cerrada en error, y con el motivo a la vista: si
    # alguien deja de recibir avisos durante semanas, tiene que verse en el
    # historial de la alerta, y el aviso debe poder salir cuando se reactive.
    assert resultado.estado_envio == ESTADO_PENDIENTE
    assert resultado.detalle_error is not None and "bloqueado" in resultado.detalle_error


# --- Fallos del proveedor ------------------------------------------------------


@pytest.mark.asyncio
async def test_un_fallo_de_ses_no_propaga_y_deja_el_aviso_en_cola(gestor: Sesion) -> None:
    """Un proveedor caído no puede tumbar la pasada de alertas. Y como el aviso
    sigue debiendo salir, la ejecución no se cierra: se queda en la cola."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns()])
    enviador = EnviadorDePrueba(fallo=ErrorDeEnvio("SES no disponible"))

    resultado = await _avisar(ejecucion_id, enviador)

    assert resultado.estado_envio == ESTADO_PENDIENTE
    assert resultado.detalle_error is not None
    assert "SES no disponible" in resultado.detalle_error

    async with AsyncSessionLocal() as db:
        ejecucion = await db.get(AlertaEjecucion, ejecucion_id)
        assert ejecucion is not None
        assert ejecucion.estado_envio == ESTADO_PENDIENTE
        assert ejecucion.convocatorias_encontradas == 1


@pytest.mark.asyncio
async def test_el_estado_error_no_lo_escribe_el_envio(gestor: Sesion) -> None:
    """`error` significa una sola cosa: la ejecución no llegó a término, y eso
    lo decide el motor. Un fallo de envío nunca lo escribe, porque las
    convocatorias sí quedaron registradas y la consecuencia es la contraria."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns()])

    resultado = await _avisar(ejecucion_id, EnviadorDePrueba(fallo=ErrorDeEnvio("caído")))

    assert resultado.estado_envio != "error"


@pytest.mark.asyncio
async def test_un_aviso_fallido_puede_reintentarse(gestor: Sesion) -> None:
    """Al quedarse en `pendiente_envio`, un segundo intento lo recoge. Hoy
    nadie barre esa cola, pero el dato ya lo permite."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    ejecucion_id = await _ejecutar(alerta_id, [_de_la_bdns("Kit Digital")])

    await _avisar(ejecucion_id, EnviadorDePrueba(fallo=ErrorDeEnvio("SES no disponible")))

    segundo = EnviadorDePrueba()
    resultado = await _avisar(ejecucion_id, segundo)

    assert resultado.estado_envio == ESTADO_ENVIADO
    assert len(segundo.correos) == 1
    assert "Kit Digital" in segundo.correos[0].html
    # El detalle del intento fallido se limpia al salir bien.
    assert resultado.detalle_error is None


@pytest.mark.asyncio
async def test_un_fallo_de_envio_no_hace_que_se_re_avise(gestor: Sesion) -> None:
    """Las convocatorias las registró el motor antes de intentar el envío, así
    que siguen contando como ya notificadas: el precio de un fallo es ese
    aviso, no un duplicado en el ciclo siguiente."""
    alerta_id = await crear_alerta_en_bd(gestor.id)
    convocatoria = _de_la_bdns()
    ejecucion_id = await _ejecutar(alerta_id, [convocatoria])
    await _avisar(ejecucion_id, EnviadorDePrueba(fallo=ErrorDeEnvio("SES no disponible")))

    # El motor vuelve a pasar y la BDNS le devuelve lo mismo.
    segunda_id = await _ejecutar(alerta_id, [convocatoria])

    async with AsyncSessionLocal() as db:
        segunda = await db.get(AlertaEjecucion, segunda_id)
        assert segunda is not None
        assert segunda.convocatorias_encontradas == 0
        assert segunda.estado_envio == ESTADO_SIN_NOVEDADES


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


# --- Consulta auxiliar ---------------------------------------------------------


@pytest.mark.asyncio
async def test_convocatorias_de_solo_devuelve_las_de_esa_ejecucion(gestor: Sesion) -> None:
    from app.services.notificaciones import convocatorias_de

    alerta_id = await crear_alerta_en_bd(gestor.id)
    primera = _de_la_bdns("De la primera pasada")
    segunda = _de_la_bdns("De la segunda pasada")
    ejecucion_1 = await _ejecutar(alerta_id, [primera])
    ejecucion_2 = await _ejecutar(alerta_id, [segunda])

    async with AsyncSessionLocal() as db:
        titulos_1 = [c.titulo for c in await convocatorias_de(db, ejecucion_1)]
        titulos_2 = [c.titulo for c in await convocatorias_de(db, ejecucion_2)]

    assert titulos_1 == ["De la primera pasada"]
    assert titulos_2 == ["De la segunda pasada"]


@pytest.mark.asyncio
async def test_la_convocatoria_queda_cacheada_con_los_datos_de_la_bdns(gestor: Sesion) -> None:
    alerta_id = await crear_alerta_en_bd(gestor.id)
    convocatoria = _de_la_bdns("Ayudas I+D+i", nivel3="Agencia IDEA")
    await _ejecutar(alerta_id, [convocatoria])

    async with AsyncSessionLocal() as db:
        cacheada = (
            await db.execute(
                select(Convocatoria).where(Convocatoria.codigo_bdns == convocatoria.codigo_bdns)
            )
        ).scalar_one()
    assert cacheada.titulo == "Ayudas I+D+i"
    assert cacheada.organo_convocante == "Agencia IDEA"
    assert cacheada.nivel_administracion == "estado"
