"""Deduplicación y registro de novedades (Hito 4, tarea 3; `bdns-local-sync` PR8).

Contra Postgres real: lo que se prueba aquí es sobre todo qué garantiza la base
de datos (el `UNIQUE(alerta_id, convocatoria_id)`), y eso con mocks no se
demuestra. Las novedades salen de la tabla local de convocatorias: cada test
siembra las suyas con `codigo_bdns_de_prueba()` (para que el teardown las
limpie) y un órgano propio, de modo que el filtro nunca toca filas reales.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria
from app.services import deduplicacion_alertas
from app.services.busqueda_convocatorias import FiltrosBusqueda
from app.services.deduplicacion_alertas import registrar_ejecucion, seleccionar_nuevas
from app.services.sistema import id_usuario_sistema
from tests.conftest import Sesion, crear_convocatoria_en_bd

# Órgano que solo usan estos tests: aísla las filas sembradas de las reales.
ORGANO_PRUEBA = 2_000_000_011
FILTROS = FiltrosBusqueda(organo=[ORGANO_PRUEBA])


def _ahora() -> datetime:
    return datetime.now(UTC)


async def _sembrar(**campos: object) -> str:
    """Una convocatoria local ya con detalle completo (`disponible_at` = ahora)."""
    ficha = {"organo_ids": [ORGANO_PRUEBA], "disponible_at": _ahora(), **campos}
    return (await crear_convocatoria_en_bd(**ficha)).codigo_bdns


async def _crear_alerta(usuario_id: int, nombre: str = "Alerta con novedades") -> Alerta:
    async with AsyncSessionLocal() as db:
        alerta = Alerta(
            usuario_id=usuario_id, nombre=nombre, texto_busqueda="digitalización", created_by=usuario_id
        )
        db.add(alerta)
        await db.commit()
        db.expunge(alerta)
        return alerta


async def _procesar(alerta: Alerta, *, desde: datetime | None = None) -> AlertaEjecucion:
    """Un ciclo de selección local y registro, como lo encadena el motor."""
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        nuevas = await seleccionar_nuevas(db, alerta.id, FILTROS, desde or _ahora() - timedelta(days=1))
        return await registrar_ejecucion(db, alerta, nuevas, usuario_sistema_id=sistema_id)


async def _notificadas(alerta_id: int) -> list[str]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(Convocatoria.codigo_bdns)
                .join(
                    AlertaEjecucionConvocatoria,
                    AlertaEjecucionConvocatoria.convocatoria_id == Convocatoria.id,
                )
                .where(AlertaEjecucionConvocatoria.alerta_id == alerta_id)
                .order_by(AlertaEjecucionConvocatoria.id)
            )
        )


async def _filas_puente(alerta_id: int) -> int:
    async with AsyncSessionLocal() as db:
        cuantas = await db.scalar(
            select(func.count())
            .select_from(AlertaEjecucionConvocatoria)
            .where(AlertaEjecucionConvocatoria.alerta_id == alerta_id)
        )
    return cuantas or 0


async def _ejecuciones(alerta_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return (
            await db.scalar(
                select(func.count()).select_from(AlertaEjecucion).where(AlertaEjecucion.alerta_id == alerta_id)
            )
            or 0
        )


# --- Primera y segunda ejecución ---------------------------------------


async def test_la_primera_ejecucion_registra_todo_lo_encontrado(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    codigos = [await _sembrar() for _ in range(3)]

    ejecucion = await _procesar(alerta)

    assert ejecucion.convocatorias_encontradas == 3
    # No se ha enviado nada todavía: el aviso es otra funcionalidad.
    assert ejecucion.estado_envio == "pendiente_envio"
    assert sorted(await _notificadas(alerta.id)) == sorted(codigos)


async def test_la_segunda_ejecucion_con_lo_mismo_no_trae_novedades(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    for _ in range(2):
        await _sembrar()

    await _procesar(alerta)
    segunda = await _procesar(alerta)

    assert segunda.convocatorias_encontradas == 0
    assert segunda.estado_envio == "sin_novedades"
    assert await _filas_puente(alerta.id) == 2


async def test_solo_lo_que_aparece_por_primera_vez_es_novedad(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    viejo = await _sembrar()
    await _procesar(alerta)
    nuevo = await _sembrar()

    segunda = await _procesar(alerta)

    assert segunda.convocatorias_encontradas == 1
    async with AsyncSessionLocal() as db:
        de_la_segunda = list(
            await db.scalars(
                select(Convocatoria.codigo_bdns)
                .join(
                    AlertaEjecucionConvocatoria,
                    AlertaEjecucionConvocatoria.convocatoria_id == Convocatoria.id,
                )
                .where(AlertaEjecucionConvocatoria.alerta_ejecucion_id == segunda.id)
            )
        )
    assert de_la_segunda == [nuevo]
    assert viejo in await _notificadas(alerta.id)


async def test_un_ciclo_sin_resultados_deja_constancia(gestor: Sesion) -> None:
    """Que no haya nada no es un error: la ejecución se registra igual."""
    alerta = await _crear_alerta(gestor.id)
    ejecucion = await _procesar(alerta)

    assert (ejecucion.convocatorias_encontradas, ejecucion.estado_envio) == (0, "sin_novedades")
    assert await _ejecuciones(alerta.id) == 1


# --- Ventana `disponible_at` y filas que no cuentan ----------------------


async def test_solo_cuenta_lo_disponible_despues_de_la_ultima_ejecucion(gestor: Sesion) -> None:
    """`disponible_at > desde`: lo anterior ya lo vio la ejecución previa, y lo
    que aún no tiene detalle completo (`disponible_at` nulo) no es candidato."""
    alerta = await _crear_alerta(gestor.id)
    desde = _ahora() - timedelta(hours=1)
    despues = await _sembrar(disponible_at=_ahora())
    await _sembrar(disponible_at=desde - timedelta(hours=1))
    await _sembrar(disponible_at=None)

    ejecucion = await _procesar(alerta, desde=desde)

    assert ejecucion.convocatorias_encontradas == 1
    assert await _notificadas(alerta.id) == [despues]


async def test_una_convocatoria_retirada_no_es_novedad(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    await _sembrar(removed_at=_ahora())

    assert (await _procesar(alerta)).convocatorias_encontradas == 0


async def test_los_filtros_compartidos_acotan_las_novedades(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    con_mrr = await _sembrar(financiada_mrr=True)
    await _sembrar(financiada_mrr=False)

    async with AsyncSessionLocal() as db:
        nuevas = await seleccionar_nuevas(
            db,
            alerta.id,
            FiltrosBusqueda(organo=[ORGANO_PRUEBA], solo_mrr=True),
            _ahora() - timedelta(days=1),
        )

    assert list(nuevas) == [con_mrr]


# --- Alcance: por alerta ------------------------------------------------


async def test_la_misma_convocatoria_es_novedad_para_dos_alertas(gestor: Sesion) -> None:
    """La dedup es por alerta: son personales e independientes, incluso las dos
    del mismo usuario."""
    primera = await _crear_alerta(gestor.id, "Primera")
    segunda = await _crear_alerta(gestor.id, "Segunda")
    codigo = await _sembrar()

    de_la_primera = await _procesar(primera)
    de_la_segunda = await _procesar(segunda)

    assert de_la_primera.convocatorias_encontradas == 1
    assert de_la_segunda.convocatorias_encontradas == 1
    assert await _notificadas(primera.id) == [codigo]
    assert await _notificadas(segunda.id) == [codigo]


# --- Garantías de la base de datos -------------------------------------


async def test_dos_ciclos_simultaneos_no_registran_lo_mismo_dos_veces(gestor: Sesion) -> None:
    """Sesiones distintas y a la vez, como dos workers. Lo que impide el
    duplicado es el UNIQUE, no el código."""
    alerta = await _crear_alerta(gestor.id)
    for _ in range(2):
        await _sembrar()

    primera, segunda = await asyncio.gather(_procesar(alerta), _procesar(alerta), return_exceptions=True)
    registradas = [e.convocatorias_encontradas for e in (primera, segunda) if isinstance(e, AlertaEjecucion)]
    assert sum(registradas) == 2
    assert await _filas_puente(alerta.id) == 2


async def test_si_el_registro_falla_nada_queda_como_visto(
    gestor: Sesion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo contrario perdería el resultado para siempre: en el ciclo siguiente ya
    no aparecería como nuevo."""
    alerta = await _crear_alerta(gestor.id)
    await _sembrar()

    async def revienta(*args: object, **kwargs: object) -> list[int]:
        raise RuntimeError("se cae justo antes del commit")

    monkeypatch.setattr(deduplicacion_alertas, "_marcar_notificadas", revienta)
    with pytest.raises(RuntimeError):
        await _procesar(alerta)
    monkeypatch.undo()

    assert await _ejecuciones(alerta.id) == 0
    assert await _filas_puente(alerta.id) == 0

    assert (await _procesar(alerta)).convocatorias_encontradas == 1
