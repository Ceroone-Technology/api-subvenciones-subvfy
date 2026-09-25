"""Deduplicación y registro de resultados (Hito 4, tarea 3).

Contra Postgres real: lo que se prueba aquí es sobre todo qué garantiza la base
de datos (el `UNIQUE(alerta_id, convocatoria_id)`), y eso con mocks no se
demuestra. Las convocatorias de prueba usan `codigo_bdns_de_prueba()` para que
el teardown las limpie.
"""

import asyncio
from datetime import date

import pytest
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria
from app.services import deduplicacion_alertas
from app.services.bdns_cliente import ConvocatoriaBdns
from app.services.deduplicacion_alertas import filtrar_nuevas, registrar_ejecucion
from app.services.sistema import id_usuario_sistema
from tests.conftest import Sesion, codigo_bdns_de_prueba


def _resultado(codigo: str, *, titulo: str = "Convocatoria detectada", mrr: bool = False) -> ConvocatoriaBdns:
    return ConvocatoriaBdns(
        id_bdns=abs(hash(codigo)) % 10**8,
        codigo_bdns=codigo,
        titulo=titulo,
        fecha_registro=date(2026, 9, 20),
        nivel1="AUTONOMICA",
        nivel2="JUNTA DE ANDALUCIA",
        nivel3="CONSEJERIA DE EMPLEO",
        financiada_mrr=mrr,
    )


async def _crear_alerta(usuario_id: int, nombre: str = "Alerta con novedades") -> Alerta:
    async with AsyncSessionLocal() as db:
        alerta = Alerta(
            usuario_id=usuario_id, nombre=nombre, texto_busqueda="digitalización", created_by=usuario_id
        )
        db.add(alerta)
        await db.commit()
        db.expunge(alerta)
        return alerta


async def _procesar(alerta: Alerta, resultados: list[ConvocatoriaBdns]) -> AlertaEjecucion:
    """Un ciclo de dedup y registro, como lo encadena el motor."""
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        nuevas = await filtrar_nuevas(db, alerta.id, resultados)
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


# --- Primera y segunda ejecución ---------------------------------------


async def test_la_primera_ejecucion_registra_todo_lo_encontrado(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    codigos = [codigo_bdns_de_prueba() for _ in range(3)]

    ejecucion = await _procesar(alerta, [_resultado(c) for c in codigos])

    assert ejecucion.convocatorias_encontradas == 3
    # No se ha enviado nada todavía: el aviso es otra funcionalidad.
    assert ejecucion.estado_envio == "pendiente_envio"
    assert sorted(await _notificadas(alerta.id)) == sorted(codigos)

    # Y las convocatorias quedan en la caché local, firmadas por el sistema.
    async with AsyncSessionLocal() as db:
        sistema_id = await id_usuario_sistema(db)
        cacheada = (
            await db.execute(select(Convocatoria).where(Convocatoria.codigo_bdns == codigos[0]))
        ).scalar_one()
    assert cacheada.titulo == "Convocatoria detectada"
    assert cacheada.nivel_administracion == "ccaa"  # AUTONOMICA, traducido a lo nuestro
    assert cacheada.administracion == "JUNTA DE ANDALUCIA"
    assert cacheada.organo_convocante == "CONSEJERIA DE EMPLEO"
    assert cacheada.created_by == sistema_id


async def test_la_segunda_ejecucion_con_lo_mismo_no_trae_novedades(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    resultados = [_resultado(codigo_bdns_de_prueba()) for _ in range(2)]

    await _procesar(alerta, resultados)
    segunda = await _procesar(alerta, resultados)

    assert segunda.convocatorias_encontradas == 0
    assert segunda.estado_envio == "sin_novedades"
    # Sin filas duplicadas: siguen siendo dos.
    assert await _filas_puente(alerta.id) == 2


async def test_solo_lo_que_aparece_por_primera_vez_es_novedad(gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    viejo, nuevo = codigo_bdns_de_prueba(), codigo_bdns_de_prueba()

    await _procesar(alerta, [_resultado(viejo)])
    segunda = await _procesar(alerta, [_resultado(viejo), _resultado(nuevo)])

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


async def test_un_ciclo_sin_resultados_deja_constancia(gestor: Sesion) -> None:
    """Que no haya nada no es un error: la ejecución se registra igual."""
    alerta = await _crear_alerta(gestor.id)
    ejecucion = await _procesar(alerta, [])

    assert (ejecucion.convocatorias_encontradas, ejecucion.estado_envio) == (0, "sin_novedades")
    async with AsyncSessionLocal() as db:
        cuantas = await db.scalar(
            select(func.count()).select_from(AlertaEjecucion).where(AlertaEjecucion.alerta_id == alerta.id)
        )
    assert cuantas == 1


# --- Alcance: por alerta ------------------------------------------------


async def test_la_misma_convocatoria_es_novedad_para_dos_alertas(gestor: Sesion) -> None:
    """La dedup es por alerta: son personales e independientes, incluso las dos
    del mismo usuario."""
    primera = await _crear_alerta(gestor.id, "Primera")
    segunda = await _crear_alerta(gestor.id, "Segunda")
    codigo = codigo_bdns_de_prueba()

    de_la_primera = await _procesar(primera, [_resultado(codigo)])
    de_la_segunda = await _procesar(segunda, [_resultado(codigo)])

    assert de_la_primera.convocatorias_encontradas == 1
    assert de_la_segunda.convocatorias_encontradas == 1
    assert await _notificadas(primera.id) == [codigo]
    assert await _notificadas(segunda.id) == [codigo]


async def test_los_duplicados_del_mismo_lote_se_registran_una_vez(gestor: Sesion) -> None:
    """La BDNS puede repetir un código entre páginas."""
    alerta = await _crear_alerta(gestor.id)
    codigo = codigo_bdns_de_prueba()

    ejecucion = await _procesar(alerta, [_resultado(codigo), _resultado(codigo), _resultado(codigo)])

    assert ejecucion.convocatorias_encontradas == 1
    assert await _filas_puente(alerta.id) == 1


# --- Garantías de la base de datos -------------------------------------


async def test_dos_ciclos_simultaneos_no_registran_lo_mismo_dos_veces(gestor: Sesion) -> None:
    """Sesiones distintas y a la vez, como dos workers. Lo que impide el
    duplicado es el UNIQUE, no el código."""
    alerta = await _crear_alerta(gestor.id)
    resultados = [_resultado(codigo_bdns_de_prueba()) for _ in range(2)]

    primera, segunda = await asyncio.gather(
        _procesar(alerta, resultados), _procesar(alerta, resultados), return_exceptions=True
    )
    # Una de las dos puede chocar y abortar; lo que no puede pasar es que las
    # dos registren las mismas convocatorias.
    registradas = [e.convocatorias_encontradas for e in (primera, segunda) if isinstance(e, AlertaEjecucion)]
    assert sum(registradas) == 2
    assert await _filas_puente(alerta.id) == 2


async def test_si_el_registro_falla_nada_queda_como_visto(
    gestor: Sesion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo contrario perdería el resultado para siempre: en el ciclo siguiente ya
    no aparecería como nuevo."""
    alerta = await _crear_alerta(gestor.id)
    codigo = codigo_bdns_de_prueba()

    async def revienta(*args: object, **kwargs: object) -> list[int]:
        raise RuntimeError("se cae justo antes del commit")

    monkeypatch.setattr(deduplicacion_alertas, "_marcar_notificadas", revienta)
    with pytest.raises(RuntimeError):
        await _procesar(alerta, [_resultado(codigo)])
    monkeypatch.undo()

    # Ni ejecución ni filas puente: la transacción se fue entera.
    async with AsyncSessionLocal() as db:
        ejecuciones = await db.scalar(
            select(func.count()).select_from(AlertaEjecucion).where(AlertaEjecucion.alerta_id == alerta.id)
        )
    assert ejecuciones == 0
    assert await _filas_puente(alerta.id) == 0

    # Y en el ciclo siguiente vuelve a salir como novedad.
    reintento = await _procesar(alerta, [_resultado(codigo)])
    assert reintento.convocatorias_encontradas == 1


# --- Convivencia con la caché de favoritos -----------------------------


async def test_el_upsert_no_borra_lo_que_ya_habia_cacheado_un_favorito(gestor: Sesion) -> None:
    """Misma regla que en favoritos: un guardado parcial no machaca con NULL los
    datos de una sincronización anterior."""
    alerta = await _crear_alerta(gestor.id)
    codigo = codigo_bdns_de_prueba()
    async with AsyncSessionLocal() as db:
        db.add(
            Convocatoria(
                codigo_bdns=codigo,
                titulo="Ficha completa de la BDNS",
                url_portal_oficial="https://www.infosubvenciones.es/ficha",
                organo_convocante="Organo ya guardado",
                created_by=gestor.id,
            )
        )
        await db.commit()

    # La BDNS devuelve la ficha incompleta: sin url, sin niveles y sin fecha.
    incompleta = ConvocatoriaBdns(
        id_bdns=1,
        codigo_bdns=codigo,
        titulo="Título abreviado",
        fecha_registro=None,
        nivel1=None,
        nivel2=None,
        nivel3=None,
        financiada_mrr=False,
    )
    await _procesar(alerta, [incompleta])

    async with AsyncSessionLocal() as db:
        cacheada = (
            await db.execute(select(Convocatoria).where(Convocatoria.codigo_bdns == codigo))
        ).scalar_one()
    assert cacheada.titulo == "Título abreviado"  # sí se actualiza lo que viene
    assert cacheada.url_portal_oficial == "https://www.infosubvenciones.es/ficha"  # no se pierde
    assert cacheada.organo_convocante == "Organo ya guardado"
