"""Motor de ejecución de alertas (Hito 4, tarea 1): selección y ciclo.

Se prueba el servicio de dominio directamente, con su sesión, porque es como
lo llamará el scheduler en local y la Lambda en el Hito 7. Las alertas se
insertan en BD con un `ultima_ejecucion_at` explícito: es la única forma de
comprobar la frecuencia sin esperar un día.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
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
    Convocatoria,
    Usuario,
)
from app.services import deduplicacion_alertas, motor_alertas
from app.services.bdns_cliente import ClienteBdns
from app.services.bdns_sync_cliente import ClienteBdnsSync
from app.services.deduplicacion_alertas import MAX_DETALLE_ERROR, detalle_de_error, espera_reintento, registrar_fallo
from app.services.motor_alertas import (
    Evaluador,
    ResumenCiclo,
    alertas_pendientes,
    evaluar_alerta,
    procesar_alertas_pendientes,
)
from app.services.sistema import EMAIL_SISTEMA, id_usuario_sistema
from tests.conftest import Sesion, crear_convocatoria_en_bd

AHORA = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

# Criterio por defecto de las alertas de prueba: no casa con ninguna fila real de
# la tabla local, así que los ciclos de estos tests no tocan datos ajenos.
TEXTO_SIN_COINCIDENCIAS = "xqzmotoralertas"
ORGANO_A, ORGANO_B = 2_000_000_021, 2_000_000_022


async def _crear_alerta(
    usuario_id: int,
    *,
    frecuencia: str = "diaria",
    activa: bool = True,
    ultima_ejecucion_at: datetime | None = None,
    nombre: str = "Alerta del motor",
    texto_busqueda: str | None = TEXTO_SIN_COINCIDENCIAS,
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


@pytest.fixture
def bdns_bloqueada(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Cualquier llamada a la BDNS (red o clientes) falla y queda anotada.

    Un ciclo de alertas lee solo la tabla local: la lista que devuelve debe
    quedar vacía.
    """
    llamadas: list[str] = []

    def bloquear(nombre: str):  # noqa: ANN202
        async def llamada(*args: object, **kwargs: object) -> None:
            llamadas.append(nombre)
            raise AssertionError(f"el motor de alertas llamó a la BDNS ({nombre})")

        return llamada

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", bloquear("red"))
    monkeypatch.setattr(ClienteBdns, "buscar_todo", bloquear("ClienteBdns.buscar_todo"))
    monkeypatch.setattr(ClienteBdnsSync, "__aenter__", bloquear("ClienteBdnsSync"))
    return llamadas


async def _sembrar(**campos: object) -> str:
    """Convocatoria local con detalle completo, disponible desde ahora."""
    ficha = {"disponible_at": datetime.now(UTC), **campos}
    return (await crear_convocatoria_en_bd(**ficha)).codigo_bdns


async def _filtrar_alerta(alerta_id: int, *, organo: int, region: int | None = None) -> None:
    async with AsyncSessionLocal() as db:
        db.add(AlertaOrgano(alerta_id=alerta_id, organo_bdns_id=organo))
        if region is not None:
            db.add(AlertaRegion(alerta_id=alerta_id, region_bdns_id=region))
        await db.commit()


async def _notificadas(alerta_id: int) -> list[str]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(Convocatoria.codigo_bdns)
                .join(AlertaEjecucionConvocatoria, AlertaEjecucionConvocatoria.convocatoria_id == Convocatoria.id)
                .where(AlertaEjecucionConvocatoria.alerta_id == alerta_id)
            )
        )


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
    gestor: Sesion, bdns_bloqueada: list[str]
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


async def test_el_evaluador_busca_en_la_tabla_local_con_los_criterios_de_la_alerta(
    gestor: Sesion, bdns_bloqueada: list[str]
) -> None:
    """Órganos y regiones (ids BDNS) filtran sobre la tabla local, y la
    ejecución queda registrada sin llamar a la BDNS."""
    alerta_id = await _crear_alerta(gestor.id, texto_busqueda=None)
    await _filtrar_alerta(alerta_id, organo=ORGANO_A, region=9)
    casa = await _sembrar(organo_ids=[ORGANO_A], region_ids=[9, 1])
    await _sembrar(organo_ids=[ORGANO_A], region_ids=[10])  # otra región
    await _sembrar(organo_ids=[ORGANO_B], region_ids=[9])  # otro órgano

    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, alerta_id)
        assert alerta is not None
        sistema_id = await id_usuario_sistema(db)
        assert await evaluar_alerta(db, alerta, sistema_id) is None

    assert await _notificadas(alerta_id) == [casa]
    assert bdns_bloqueada == []


async def test_un_ciclo_de_alertas_no_llama_a_la_bdns(gestor: Sesion, bdns_bloqueada: list[str]) -> None:
    """Escenario del spec: 2 alertas y 5 convocatorias ingeridas desde su
    última ejecución; cada alerta recibe las suyas y no sale ninguna petición."""
    ultima = datetime.now(UTC) - timedelta(days=2)
    alerta_a = await _crear_alerta(gestor.id, nombre="A", texto_busqueda=None, ultima_ejecucion_at=ultima)
    alerta_b = await _crear_alerta(gestor.id, nombre="B", texto_busqueda=None, ultima_ejecucion_at=ultima)
    await _filtrar_alerta(alerta_a, organo=ORGANO_A)
    await _filtrar_alerta(alerta_b, organo=ORGANO_B)
    de_a = [await _sembrar(organo_ids=[ORGANO_A]) for _ in range(3)]
    de_b = [await _sembrar(organo_ids=[ORGANO_B]) for _ in range(2)]
    await _sembrar(organo_ids=[ORGANO_A], disponible_at=ultima - timedelta(days=1))  # ya estaba

    resumen = await _procesar(ahora=datetime.now(UTC))

    assert (resumen.evaluadas, resumen.fallidas) == (2, 0)
    assert sorted(await _notificadas(alerta_a)) == sorted(de_a)
    assert sorted(await _notificadas(alerta_b)) == sorted(de_b)
    assert bdns_bloqueada == []


async def test_una_alerta_sin_criterios_no_frena_el_ciclo(
    gestor: Sesion, bdns_bloqueada: list[str]
) -> None:
    """Consultarla traería la BDNS entera, así que se cuenta como fallida y se
    reintentará; la siguiente alerta se procesa igual."""
    sin_criterios = await _crear_alerta(gestor.id, nombre="Sin criterios", texto_busqueda=None)
    con_criterios = await _crear_alerta(gestor.id, nombre="Con criterios")

    resumen = await _procesar()
    assert (resumen.evaluadas, resumen.fallidas) == (1, 1)
    assert (await _alerta_en_bd(con_criterios)).ultima_ejecucion_at == AHORA
    assert (await _alerta_en_bd(sin_criterios)).ultima_ejecucion_at is None
    assert bdns_bloqueada == []


async def test_el_ciclo_registra_las_novedades_y_no_las_repite(
    gestor: Sesion, bdns_bloqueada: list[str]
) -> None:
    """De punta a punta: el ciclo selecciona en local, deduplica y escribe el historial que
    lee GET /alertas/{id}/ejecuciones. Al repetirlo, nada nuevo."""
    await _sembrar(titulo=f"Ayudas {TEXTO_SIN_COINCIDENCIAS}", nivel_administracion="estado")
    alerta_id = await _crear_alerta(gestor.id)

    primera = await _procesar()
    assert primera.evaluadas == 1

    # Un día después la alerta vuelve a tocar y la fila sigue ahí, así que esta
    # vez no hay novedad.
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
    # "enviado" y no "pendiente_envio": desde el Hito 4 F3 el ciclo encadena el
    # aviso (evaluar_alerta llama a enviar_aviso), que cierra la ejecucion. En
    # tests el backend de correo es "consola", asi que no sale nada a la red.
    assert ejecuciones == [(1, "enviado"), (0, "sin_novedades")]
    assert filas_puente == 1


# --- Fallos y reintentos ------------------------------------------------


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
    gestor: Sesion, client_gestor: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El caso del issue: el rollback del registro se llevaba la fila de
    `alerta_ejecucion` y la alerta no dejaba rastro."""
    await _sembrar(titulo=f"Ayudas {TEXTO_SIN_COINCIDENCIAS}")
    alerta_id = await _crear_alerta(gestor.id)

    async def registro_que_rechaza_postgres(db: AsyncSession, *args: object, **kwargs: object) -> list[int]:
        # Fallo real de la base de datos cuyo mensaje arrastra el parámetro.
        await db.execute(text("SELECT CAST(:valor AS text)"), {"valor": "SECRETO-DEL-TITULO\x00"})
        return []

    monkeypatch.setattr(deduplicacion_alertas, "_marcar_notificadas", registro_que_rechaza_postgres)

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
    gestor: Sesion, bdns_bloqueada: list[str]
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


async def test_si_falla_el_marcado_la_alerta_deja_rastro_y_el_ciclo_sigue(
    gestor: Sesion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_marcar_ejecutada` estaba fuera del `try`: su fallo abortaba el resto
    del lote y no dejaba fila de error ni espera."""
    rota = await _crear_alerta(gestor.id, nombre="Falla al marcarse")
    sana = await _crear_alerta(gestor.id, nombre="Se marca bien")
    marcar_de_verdad = motor_alertas._marcar_ejecutada

    async def marcado_roto(db: AsyncSession, alerta_id: int, usuario_sistema_id: int, ahora: datetime) -> None:
        if alerta_id == rota:
            raise ConnectionError("la base de datos se ha caído")
        await marcar_de_verdad(db, alerta_id, usuario_sistema_id, ahora)

    async def evaluador(db: AsyncSession, alerta: Alerta, usuario_sistema_id: int) -> None:
        return None

    monkeypatch.setattr(motor_alertas, "_marcar_ejecutada", marcado_roto)

    resumen = await _procesar(evaluador)

    assert (resumen.evaluadas, resumen.fallidas) == (1, 1)
    [(_, estado, detalle)] = await _ejecuciones_de(rota)
    assert estado == "error"
    assert detalle is not None and "ConnectionError" in detalle
    alerta_rota = await _alerta_en_bd(rota)
    assert alerta_rota.ultima_ejecucion_at is None
    assert alerta_rota.fallos_consecutivos == 1
    assert alerta_rota.proximo_reintento_at == AHORA + timedelta(minutes=15)
    # El ciclo no se cortó: la siguiente del lote se evaluó y se marcó.
    assert (await _alerta_en_bd(sana)).ultima_ejecucion_at == AHORA


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
