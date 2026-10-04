"""Motor de ejecución de alertas (Hito 4, tarea 1): selección y ciclo.

Se prueba el servicio de dominio directamente, con su sesión, porque es como
lo llamará el scheduler en local y la Lambda en el Hito 7. Las alertas se
insertan en BD con un `ultima_ejecucion_at` explícito: es la única forma de
comprobar la frecuencia sin esperar un día.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.scheduler import JOB_MOTOR_ALERTAS, iniciar_scheduler, parar_scheduler
from app.database import AsyncSessionLocal
from app.models import (
    Alerta,
    AlertaEjecucion,
    AlertaEjecucionConvocatoria,
    AlertaOrgano,
    AlertaRegion,
    Usuario,
)
from app.services import motor_alertas
from app.services.bdns_cliente import ConvocatoriaBdns
from app.services.bdns_consulta import ConsultaBdns
from app.services.deduplicacion_alertas import MAX_DETALLE_ERROR, detalle_de_error, espera_reintento, registrar_fallo
from app.services.motor_alertas import (
    Evaluador,
    ResumenCiclo,
    alertas_pendientes,
    evaluar_alerta,
    procesar_alertas_pendientes,
)
from app.services.sistema import EMAIL_SISTEMA, id_usuario_sistema
from tests.conftest import Sesion, codigo_bdns_de_prueba

AHORA = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


async def _crear_alerta(
    usuario_id: int,
    *,
    frecuencia: str = "diaria",
    activa: bool = True,
    ultima_ejecucion_at: datetime | None = None,
    nombre: str = "Alerta del motor",
    texto_busqueda: str | None = "digitalización",
    fallos_consecutivos: int = 0,
    proximo_reintento_at: datetime | None = None,
) -> int:
    """Con un criterio por defecto: una alerta sin ninguno no se puede
    consultar en la BDNS, y eso tiene su propio test."""
    async with AsyncSessionLocal() as db:
        alerta = Alerta(
            usuario_id=usuario_id,
            nombre=nombre,
            frecuencia=frecuencia,
            activa=activa,
            ultima_ejecucion_at=ultima_ejecucion_at,
            texto_busqueda=texto_busqueda,
            fallos_consecutivos=fallos_consecutivos,
            proximo_reintento_at=proximo_reintento_at,
            created_by=usuario_id,
        )
        db.add(alerta)
        await db.commit()
        return alerta.id


async def _alerta_en_bd(alerta_id: int) -> Alerta:
    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, alerta_id)
    assert alerta is not None
    return alerta


async def _ids_pendientes(ahora: datetime = AHORA) -> list[int]:
    async with AsyncSessionLocal() as db:
        return [alerta.id for alerta in await alertas_pendientes(db, ahora=ahora)]


class _ClienteBdnsFalso:
    """Sustituye a ClienteBdns en los tests del ciclo: registra las consultas
    que recibe y no toca la red."""

    consultas: list[ConsultaBdns] = []
    resultados: list[ConvocatoriaBdns] = []

    def __init__(self, convocatorias: list[ConvocatoriaBdns] | None = None) -> None:
        self._convocatorias = convocatorias if convocatorias is not None else type(self).resultados

    async def __aenter__(self) -> "_ClienteBdnsFalso":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def buscar_todo(self, consulta: ConsultaBdns) -> list[ConvocatoriaBdns]:
        type(self).consultas.append(consulta)
        return self._convocatorias


@pytest.fixture
def bdns_falsa(monkeypatch: pytest.MonkeyPatch) -> type[_ClienteBdnsFalso]:
    _ClienteBdnsFalso.consultas = []
    _ClienteBdnsFalso.resultados = []
    monkeypatch.setattr(motor_alertas, "ClienteBdns", _ClienteBdnsFalso)
    return _ClienteBdnsFalso


async def _procesar(
    evaluador: Evaluador | None = None,
    *,
    ahora: datetime = AHORA,
    reloj: Callable[[], datetime] | None = None,
) -> ResumenCiclo:
    """Por defecto, un reloj parado en `ahora`: el ciclo y sus fallos ven la
    misma hora. Para que el tiempo avance durante el ciclo, `reloj`."""
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        return await procesar_alertas_pendientes(
            db, usuario_sistema_id=sistema_id, evaluador=evaluador, reloj=reloj or (lambda: ahora)
        )


# --- Selección ----------------------------------------------------------


async def test_una_alerta_nunca_ejecutada_toca(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    assert alerta in await _ids_pendientes()


async def test_una_alerta_pausada_se_ignora(gestor: Sesion) -> None:
    pausada = await _crear_alerta(gestor.id, activa=False)
    activa = await _crear_alerta(gestor.id)

    pendientes = await _ids_pendientes()
    assert activa in pendientes
    assert pausada not in pendientes


@pytest.mark.parametrize(
    ("frecuencia", "hace", "toca"),
    [
        ("diaria", timedelta(hours=1), False),
        ("diaria", timedelta(days=1, minutes=1), True),
        ("semanal", timedelta(days=2), False),
        ("semanal", timedelta(days=7, minutes=1), True),
        # Intervalo cero: su cadencia real es la del ciclo, así que siempre toca.
        ("inmediata", timedelta(seconds=1), True),
    ],
)
async def test_la_frecuencia_decide_si_toca(
    gestor: Sesion, frecuencia: str, hace: timedelta, toca: bool
) -> None:
    alerta = await _crear_alerta(gestor.id, frecuencia=frecuencia, ultima_ejecucion_at=AHORA - hace)
    assert (alerta in await _ids_pendientes()) is toca


async def test_las_mas_atrasadas_primero(gestor: Sesion) -> None:
    """Orden de servicio: primero las que llevan más tiempo sin evaluarse, y
    antes que ninguna las que nunca se han ejecutado."""
    reciente = await _crear_alerta(gestor.id, ultima_ejecucion_at=AHORA - timedelta(days=2))
    antigua = await _crear_alerta(gestor.id, ultima_ejecucion_at=AHORA - timedelta(days=30))
    nunca = await _crear_alerta(gestor.id)

    assert await _ids_pendientes() == [nunca, antigua, reciente]


# --- Ciclo --------------------------------------------------------------


async def test_el_ciclo_marca_la_ejecucion_y_firma_como_sistema(
    gestor: Sesion, bdns_falsa: type[_ClienteBdnsFalso]
) -> None:
    alerta_id = await _crear_alerta(gestor.id)
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)

    resumen = await _procesar()
    assert resumen.evaluadas >= 1
    assert resumen.fallidas == 0

    alerta = await _alerta_en_bd(alerta_id)
    assert alerta.ultima_ejecucion_at == AHORA
    # Los procesos automáticos firman con el usuario de sistema, no con una persona.
    assert alerta.updated_by == sistema_id
    assert alerta.created_by == gestor.id

    # Y ya no vuelve a tocar en el mismo instante.
    assert alerta_id not in await _ids_pendientes()


async def test_una_alerta_que_falla_no_frena_a_las_demas(gestor: Sesion) -> None:
    rota = await _crear_alerta(gestor.id, nombre="La que revienta")
    sana = await _crear_alerta(gestor.id, nombre="La que va bien")

    async def evaluador(db: AsyncSession, alerta: Alerta, usuario_sistema_id: int) -> None:
        if alerta.id == rota:
            raise RuntimeError("la BDNS no responde")

    resumen = await _procesar(evaluador)
    assert (resumen.evaluadas, resumen.fallidas) == (1, 1)

    # La sana avanza; la rota no, y se reintenta cuando acaba su espera.
    assert (await _alerta_en_bd(sana)).ultima_ejecucion_at == AHORA
    assert (await _alerta_en_bd(rota)).ultima_ejecucion_at is None
    assert rota not in await _ids_pendientes()
    assert rota in await _ids_pendientes(AHORA + timedelta(minutes=15))


async def test_un_ciclo_sin_alertas_pendientes(usuario_raso: Sesion) -> None:
    """Sin nada que hacer no es un error: el resumen sale a cero."""
    await _crear_alerta(usuario_raso.id, activa=False)
    resumen = await _procesar()
    assert resumen == ResumenCiclo(pendientes=0, evaluadas=0, fallidas=0)


async def test_el_evaluador_consulta_la_bdns_con_los_criterios_de_la_alerta(
    gestor: Sesion, bdns_falsa: type[_ClienteBdnsFalso]
) -> None:
    """Se construye la consulta con los criterios y sus filtros, y la ejecución
    queda registrada aunque no haya resultados."""
    alerta_id = await _crear_alerta(gestor.id, texto_busqueda="pymes")
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                AlertaOrgano(alerta_id=alerta_id, organo_bdns_id=1500),
                AlertaRegion(alerta_id=alerta_id, region_bdns_id=9),
            ]
        )
        await db.commit()

    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, alerta_id)
        assert alerta is not None
        sistema_id = await id_usuario_sistema(db)
        assert await evaluar_alerta(db, alerta, sistema_id) is None

    consulta = bdns_falsa.consultas[-1]
    assert consulta.descripcion == "pymes"
    assert consulta.organos == (1500,)
    assert consulta.regiones == (9,)
    # La primera vez no hay ultima_ejecucion_at: se usa la ventana inicial.
    assert consulta.fecha_desde is not None

    async with AsyncSessionLocal() as db:
        ejecucion = (
            await db.execute(select(AlertaEjecucion).where(AlertaEjecucion.alerta_id == alerta_id))
        ).scalar_one()
    # El doble de la BDNS no devuelve nada, así que la ejecución queda sin novedades.
    assert (ejecucion.convocatorias_encontradas, ejecucion.estado_envio) == (0, "sin_novedades")


async def test_una_alerta_sin_criterios_no_frena_el_ciclo(
    gestor: Sesion, bdns_falsa: type[_ClienteBdnsFalso]
) -> None:
    """Consultarla traería la BDNS entera, así que se cuenta como fallida y se
    reintentará; la siguiente alerta se procesa igual."""
    sin_criterios = await _crear_alerta(gestor.id, nombre="Sin criterios", texto_busqueda=None)
    con_criterios = await _crear_alerta(gestor.id, nombre="Con criterios")

    resumen = await _procesar()
    assert (resumen.evaluadas, resumen.fallidas) == (1, 1)
    assert (await _alerta_en_bd(con_criterios)).ultima_ejecucion_at == AHORA
    assert (await _alerta_en_bd(sin_criterios)).ultima_ejecucion_at is None
    # Solo llegó a la BDNS la que sí tenía criterios.
    assert len(bdns_falsa.consultas) == 1


async def test_el_ciclo_registra_las_novedades_y_no_las_repite(
    gestor: Sesion, bdns_falsa: type[_ClienteBdnsFalso]
) -> None:
    """De punta a punta: el ciclo consulta, deduplica y escribe el historial que
    lee GET /alertas/{id}/ejecuciones. Al repetirlo, nada nuevo."""
    codigo = codigo_bdns_de_prueba()
    encontrada = ConvocatoriaBdns(
        id_bdns=1,
        codigo_bdns=codigo,
        titulo="Ayudas a la digitalización",
        fecha_registro=None,
        nivel1="ESTADO",
        nivel2="MINISTERIO DE INDUSTRIA",
        nivel3=None,
        financiada_mrr=True,
    )
    bdns_falsa.resultados = [encontrada]
    alerta_id = await _crear_alerta(gestor.id)

    primera = await _procesar()
    assert primera.evaluadas == 1

    # Un día después la alerta vuelve a tocar y la BDNS devuelve lo mismo, así
    # que esta vez no hay novedad.
    await _procesar(ahora=AHORA + timedelta(days=1, minutes=1))

    async with AsyncSessionLocal() as db:
        ejecuciones = list(
            await db.execute(
                select(AlertaEjecucion.convocatorias_encontradas, AlertaEjecucion.estado_envio)
                .where(AlertaEjecucion.alerta_id == alerta_id)
                .order_by(AlertaEjecucion.id)
            )
        )
        filas_puente = await db.scalar(
            select(func.count())
            .select_from(AlertaEjecucionConvocatoria)
            .where(AlertaEjecucionConvocatoria.alerta_id == alerta_id)
        )
    assert ejecuciones == [(1, "pendiente_envio"), (0, "sin_novedades")]
    assert filas_puente == 1


# --- Fallos y reintentos ------------------------------------------------


def _convocatoria_que_postgres_rechaza(titulo: str) -> ConvocatoriaBdns:
    """Pasa todos los filtros de `filtrar_nuevas` y revienta en el upsert: el
    NUL no cabe en un TEXT de Postgres. Es un fallo real de la base de datos,
    no un doble."""
    return ConvocatoriaBdns(
        id_bdns=1,
        codigo_bdns=codigo_bdns_de_prueba(),
        titulo=titulo,
        fecha_registro=None,
        nivel1="ESTADO",
        nivel2=None,
        nivel3=None,
        financiada_mrr=False,
    )


async def _ejecuciones_de(alerta_id: int) -> list[tuple[int, str, str | None]]:
    async with AsyncSessionLocal() as db:
        return [
            tuple(fila)
            for fila in await db.execute(
                select(
                    AlertaEjecucion.convocatorias_encontradas,
                    AlertaEjecucion.estado_envio,
                    AlertaEjecucion.detalle_error,
                )
                .where(AlertaEjecucion.alerta_id == alerta_id)
                .order_by(AlertaEjecucion.id)
            )
        ]


async def _siempre_falla(db: AsyncSession, alerta: Alerta, usuario_sistema_id: int) -> None:
    raise RuntimeError("la BDNS no responde")


async def test_un_fallo_del_upsert_deja_rastro_y_no_avanza_la_alerta(
    gestor: Sesion, client_gestor: AsyncClient, bdns_falsa: type[_ClienteBdnsFalso]
) -> None:
    """El caso del issue: el rollback del upsert se llevaba la fila de
    `alerta_ejecucion` y la alerta no dejaba rastro."""
    bdns_falsa.resultados = [_convocatoria_que_postgres_rechaza("SECRETO-DEL-TITULO\x00")]
    alerta_id = await _crear_alerta(gestor.id)

    resumen = await _procesar()
    assert (resumen.evaluadas, resumen.fallidas) == (0, 1)

    [(encontradas, estado, detalle)] = await _ejecuciones_de(alerta_id)
    assert (encontradas, estado) == (0, "error")
    assert detalle is not None and detalle.startswith("[ejecución] DBAPIError: ")
    # El detalle sale por la API: no puede arrastrar los parámetros del INSERT.
    assert "SECRETO-DEL-TITULO" not in detalle

    alerta = await _alerta_en_bd(alerta_id)
    # `ultima_ejecucion_at` es el `desde` de la consulta: avanzarlo perdería lo
    # publicado entre el fallo y el reintento.
    assert alerta.ultima_ejecucion_at is None
    assert alerta.fallos_consecutivos == 1
    assert alerta.proximo_reintento_at == AHORA + timedelta(minutes=15)

    async with AsyncSessionLocal() as db:
        puente = await db.scalar(
            select(func.count())
            .select_from(AlertaEjecucionConvocatoria)
            .where(AlertaEjecucionConvocatoria.alerta_id == alerta_id)
        )
    assert puente == 0

    # Y es visible en el historial que consulta el frontend.
    respuesta = await client_gestor.get(f"/alertas/{alerta_id}/ejecuciones", params={"estado_envio": "error"})
    assert respuesta.status_code == 200, respuesta.text
    assert [item["detalle_error"] for item in respuesta.json()["items"]] == [detalle]


async def test_una_alerta_con_criterios_invalidos_tambien_deja_rastro(
    gestor: Sesion, bdns_falsa: type[_ClienteBdnsFalso]
) -> None:
    alerta_id = await _crear_alerta(gestor.id, texto_busqueda=None)

    await _procesar()

    [(_, estado, detalle)] = await _ejecuciones_de(alerta_id)
    assert estado == "error"
    assert detalle is not None and "CriteriosAlertaInvalidos" in detalle


async def test_una_alerta_en_espera_no_toca_hasta_que_vence(gestor: Sesion) -> None:
    alerta_id = await _crear_alerta(gestor.id)
    await _procesar(_siempre_falla)

    assert alerta_id not in await _ids_pendientes(AHORA + timedelta(minutes=14))
    assert alerta_id in await _ids_pendientes(AHORA + timedelta(minutes=15))


async def test_fallar_otra_vez_alarga_la_espera(gestor: Sesion) -> None:
    alerta_id = await _crear_alerta(gestor.id)

    primero = AHORA
    await _procesar(_siempre_falla, ahora=primero)
    segundo = primero + timedelta(minutes=15)
    await _procesar(_siempre_falla, ahora=segundo)

    alerta = await _alerta_en_bd(alerta_id)
    assert alerta.fallos_consecutivos == 2
    assert alerta.proximo_reintento_at == segundo + timedelta(minutes=30)
    assert [estado for _, estado, _ in await _ejecuciones_de(alerta_id)] == ["error", "error"]
    # Y nunca avanzó el punto desde el que se consulta la BDNS.
    assert alerta.ultima_ejecucion_at is None


async def test_la_espera_se_cuenta_desde_el_fallo_y_no_desde_el_inicio_del_ciclo(gestor: Sesion) -> None:
    """Con la BDNS caída cada alerta agota su timeout y el lote puede durar
    más que la primera espera. Contada desde el inicio del ciclo, la marca
    nacía vencida y el tick siguiente volvía a coger la alerta."""
    lenta = await _crear_alerta(gestor.id, nombre="Agota el timeout")
    sana = await _crear_alerta(gestor.id, nombre="Va bien")
    reloj = [AHORA]

    async def evaluador(db: AsyncSession, alerta: Alerta, usuario_sistema_id: int) -> None:
        if alerta.id == lenta:
            reloj[0] += timedelta(minutes=20)
            raise RuntimeError("la BDNS no responde")

    await _procesar(evaluador, reloj=lambda: reloj[0])

    alerta_lenta = await _alerta_en_bd(lenta)
    assert alerta_lenta.proximo_reintento_at == AHORA + timedelta(minutes=20 + 15)
    assert lenta not in await _ids_pendientes(AHORA + timedelta(minutes=34))
    # Lo que sale bien se sigue marcando con la hora del ciclo, aunque se
    # evalúe después del fallo: la selección por frecuencia es del lote.
    assert (await _alerta_en_bd(sana)).ultima_ejecucion_at == AHORA


def test_la_espera_crece_al_doble_y_tiene_tope() -> None:
    minutos = [espera_reintento(n).total_seconds() / 60 for n in range(1, 10)]
    assert minutos == [15, 30, 60, 120, 240, 480, 960, 1440, 1440]
    # Sin desbordar con contadores absurdos.
    assert espera_reintento(10_000) == timedelta(hours=24)
    assert espera_reintento(0) == timedelta(minutes=15)


async def test_un_exito_reinicia_la_espera(gestor: Sesion) -> None:
    alerta_id = await _crear_alerta(
        gestor.id, fallos_consecutivos=3, proximo_reintento_at=AHORA - timedelta(minutes=1)
    )
    assert alerta_id in await _ids_pendientes()

    async def evaluador(db: AsyncSession, alerta: Alerta, usuario_sistema_id: int) -> None:
        return None

    await _procesar(evaluador)

    alerta = await _alerta_en_bd(alerta_id)
    assert (alerta.fallos_consecutivos, alerta.proximo_reintento_at) == (0, None)
    assert alerta.ultima_ejecucion_at == AHORA


async def test_una_alerta_rota_no_desplaza_a_las_sanas(gestor: Sesion) -> None:
    """Sin espera, la rota (nunca ejecutada, id menor) saldría siempre la
    primera y, con el tope por ciclo, dejaría sin turno a las demás."""
    rota = await _crear_alerta(gestor.id, nombre="Rota", proximo_reintento_at=AHORA + timedelta(hours=1))
    sana = await _crear_alerta(gestor.id, nombre="Sana")

    async with AsyncSessionLocal() as db:
        elegidas = [alerta.id for alerta in await alertas_pendientes(db, ahora=AHORA, limite=1)]
    assert elegidas == [sana]
    assert rota not in elegidas


async def test_si_falla_el_registro_del_fallo_el_ciclo_sigue(
    gestor: Sesion, monkeypatch: pytest.MonkeyPatch
) -> None:
    rota = await _crear_alerta(gestor.id, nombre="La que revienta")
    sana = await _crear_alerta(gestor.id, nombre="La que va bien")

    async def evaluador(db: AsyncSession, alerta: Alerta, usuario_sistema_id: int) -> None:
        if alerta.id == rota:
            raise RuntimeError("la BDNS no responde")

    async def registro_roto(*args: object, **kwargs: object) -> None:
        raise ConnectionError("la base de datos se ha caído")

    monkeypatch.setattr(motor_alertas, "registrar_fallo", registro_roto)

    resumen = await _procesar(evaluador)

    assert (resumen.evaluadas, resumen.fallidas) == (1, 1)
    assert (await _alerta_en_bd(sana)).ultima_ejecucion_at == AHORA
    assert (await _alerta_en_bd(rota)).ultima_ejecucion_at is None


async def test_registrar_el_fallo_de_una_alerta_borrada_no_hace_nada(gestor: Sesion) -> None:
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        ejecucion = await registrar_fallo(
            db, -1, RuntimeError("x"), usuario_sistema_id=sistema_id, ahora=AHORA
        )
    assert ejecucion is None


def test_el_detalle_del_error_es_corto_y_de_una_linea() -> None:
    largo = detalle_de_error(ValueError("línea 1\nlínea 2 con datos\n" + "x" * 2000))
    assert "\n" not in largo and "línea 2" not in largo
    assert largo.startswith("[ejecución] ValueError: línea 1")
    assert len(detalle_de_error(ValueError("x" * 2000))) == MAX_DETALLE_ERROR
    assert detalle_de_error(RuntimeError()) == "[ejecución] RuntimeError: "


# --- Usuario de sistema -------------------------------------------------


async def test_el_usuario_de_sistema_existe_y_no_es_de_acceso() -> None:
    """Lo siembra la migración b7f3c21a9d40, y nace bloqueado a propósito."""
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        estado = await db.scalar(select(Usuario.estado).where(Usuario.email == EMAIL_SISTEMA))
    assert sistema_id > 0
    assert estado == "bloqueado"


# --- Scheduler ----------------------------------------------------------


def test_el_scheduler_no_arranca_en_los_tests() -> None:
    """Por defecto está desactivado, así que la suite no dispara ciclos."""
    assert settings.scheduler_habilitado is False
    assert iniciar_scheduler() is None


async def test_el_scheduler_arranca_y_para_limpiamente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "scheduler_habilitado", True)
    monkeypatch.setattr(settings, "scheduler_intervalo_minutos", 1)

    scheduler = iniciar_scheduler()
    assert scheduler is not None
    try:
        assert scheduler.running
        assert scheduler.get_job(JOB_MOTOR_ALERTAS) is not None
    finally:
        await parar_scheduler(scheduler)
    assert not scheduler.running
    # Pararlo dos veces no revienta: el lifespan puede llamarlo sin scheduler.
    await parar_scheduler(scheduler)
    await parar_scheduler(None)
