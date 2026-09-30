"""Motor de ejecución de alertas (Hito 4, tarea 1): selección y ciclo.

Se prueba el servicio de dominio directamente, con su sesión, porque es como
lo llamará el scheduler en local y la Lambda en el Hito 7. Las alertas se
insertan en BD con un `ultima_ejecucion_at` explícito: es la única forma de
comprobar la frecuencia sin esperar un día.
"""

from datetime import UTC, datetime, timedelta

import pytest
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


async def _ids_pendientes() -> list[int]:
    async with AsyncSessionLocal() as db:
        return [alerta.id for alerta in await alertas_pendientes(db, ahora=AHORA)]


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


async def _procesar(evaluador: Evaluador | None = None, *, ahora: datetime = AHORA) -> ResumenCiclo:
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        return await procesar_alertas_pendientes(
            db, usuario_sistema_id=sistema_id, evaluador=evaluador, ahora=ahora
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

    # La sana avanza; la rota no, así que el ciclo siguiente la reintenta.
    assert (await _alerta_en_bd(sana)).ultima_ejecucion_at == AHORA
    assert (await _alerta_en_bd(rota)).ultima_ejecucion_at is None
    assert rota in await _ids_pendientes()


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
