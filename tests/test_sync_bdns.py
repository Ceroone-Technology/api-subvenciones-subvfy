"""Sync engine against the real database (PR3, tasks 3.1 and 3.4).

The BDNS is a stateful `MockTransport` (`FalsaBdns`); nothing reaches the real
service. Grants use `TST` codes and runs are signed by a test user, so the
teardown in `conftest.py` removes them.
"""

import copy
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.models import Convocatoria, SyncRun
from app.services.bdns_sync_cliente import ClienteBdnsSync
from app.services.sync_bdns import ejecutar
from tests.conftest import Sesion, codigo_bdns_de_prueba, crear_convocatoria_en_bd
from tests.fixtures_bdns import cargar_fixture

AHORA = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
HOY = date(2026, 10, 26)
AYER = HOY - timedelta(days=1)


class FalsaBdns:
    def __init__(self, tam_pagina: int = 200) -> None:
        self.grants: dict[str, dict[str, Any]] = {}
        self.tam = tam_pagina
        self.caido = False
        self.avance_minutos = 0
        self.avance_por_detalle = 0
        self.listados: list[dict[str, str]] = []
        self.detalles: list[str] = []
        self.corridas = 0

    def nueva(self, codigo: str | None = None, *, fin: str = "2026-12-31", registro: date = AYER, **extra: Any) -> str:
        codigo = codigo or codigo_bdns_de_prueba()
        detalle = copy.deepcopy(cargar_fixture("detalle_real.json"))
        detalle.update(
            codigoBDNS=codigo,
            id=len(self.grants) + 1,
            descripcion=f"Ayuda {codigo}",
            fechaRecepcion=registro.isoformat(),
            fechaFinSolicitud=fin,
            fechaInicioSolicitud="2026-01-01",
            abierto=True,
            **extra,
        )
        self.grants[codigo] = detalle
        return codigo

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        ruta, params = peticion.url.path, dict(peticion.url.params)
        if self.caido and ruta.endswith(("/busqueda", "/convocatorias")):
            return httpx.Response(503)
        if ruta.endswith("/busqueda"):
            self.listados.append(params)
            return self._pagina(params)
        if ruta.endswith("/convocatorias"):
            self.detalles.append(params["numConv"])
            self.avance_minutos += self.avance_por_detalle
            detalle = self.grants.get(params["numConv"])
            return httpx.Response(200, json=detalle) if detalle else httpx.Response(404)
        return httpx.Response(200, json=[])

    def _pagina(self, params: dict[str, str]) -> httpx.Response:
        desde = datetime.strptime(params["fechaDesde"], "%d/%m/%Y").date()
        hasta = datetime.strptime(params["fechaHasta"], "%d/%m/%Y").date()
        filas = sorted(
            (g for g in self.grants.values() if desde <= date.fromisoformat(g["fechaRecepcion"]) <= hasta),
            key=lambda g: (g["fechaRecepcion"], g["id"]),
        )
        pagina = int(params["page"])
        trozo = filas[pagina * self.tam : (pagina + 1) * self.tam]
        contenido = [
            {
                "id": g["id"],
                "mrr": False,
                "numeroConvocatoria": g["codigoBDNS"],
                "descripcion": g["descripcion"],
                "fechaRecepcion": g["fechaRecepcion"],
                "nivel1": "ESTADO",
                "nivel2": "X",
                "nivel3": "Y",
            }
            for g in trozo
        ]
        total_paginas = -(-len(filas) // self.tam)
        return httpx.Response(
            200,
            json={
                "content": contenido,
                "number": pagina,
                "totalPages": total_paginas,
                "totalElements": len(filas),
                "last": pagina + 1 >= total_paginas,
            },
        )


class RelojFalso:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def dormir(self, segundos: float) -> None:
        self.t += segundos


def cliente_de(bdns: FalsaBdns, **opciones: Any) -> ClienteBdnsSync:
    reloj = RelojFalso()
    opciones = {"intentos": 1, "umbral_breaker": 5, **opciones}
    return ClienteBdnsSync(
        httpx.AsyncClient(transport=httpx.MockTransport(bdns), base_url="https://bdns.example/api"),
        reloj=reloj,
        dormir=reloj.dormir,
        aleatorio=lambda: 0.0,
        **opciones,
    )


async def correr(bdns: FalsaBdns, admin: Sesion, **kw: Any) -> SyncRun | None:
    kw.setdefault("hoy", HOY)
    bdns.corridas += 1  # cada run empieza después de que acabara el anterior
    cliente = cliente_de(bdns, **kw.pop("opciones", {}))
    async with cliente:
        resultado = await ejecutar(
            cliente,
            usuario_id=admin.id,
            prefijo="TST",
            ahora=lambda: AHORA + timedelta(hours=bdns.corridas, minutes=bdns.avance_minutos),
            **kw,
        )
    if resultado is None:
        return None
    async with AsyncSessionLocal() as db:
        return await db.get(SyncRun, resultado.run_id)


async def fila(codigo: str) -> Convocatoria:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Convocatoria).where(Convocatoria.codigo_bdns == codigo))).scalar_one()


async def test_ingesta_nuevas_con_detalle(admin: Sesion) -> None:
    bdns = FalsaBdns()
    codigos = [bdns.nueva() for _ in range(3)]
    run = await correr(bdns, admin)
    assert run is not None and run.estado == "ok"
    assert (run.listadas, run.detalles, run.insertadas, run.actualizadas) == (3, 3, 3, 0)
    for codigo in codigos:
        convocatoria = await fila(codigo)
        assert convocatoria.fecha_fin_solicitud == date(2026, 12, 31)
        assert convocatoria.detalle_sync_at and convocatoria.disponible_at and convocatoria.contenido_hash
        assert convocatoria.created_by == admin.id
    assert run.peticiones_bdns >= 6
    # El listado se pidió ascendente (D8) y con la ventana de solape.
    assert bdns.listados[0]["direccion"] == "asc"


async def test_segunda_pasada_no_actualiza_nada(admin: Sesion) -> None:
    bdns = FalsaBdns()
    codigo = bdns.nueva()
    await correr(bdns, admin)
    antes = await fila(codigo)
    run = await correr(bdns, admin)
    assert run is not None and run.estado == "ok"
    assert (run.insertadas, run.actualizadas) == (0, 0)
    despues = await fila(codigo)
    assert despues.id == antes.id and despues.contenido_hash == antes.contenido_hash
    assert despues.disponible_at == antes.disponible_at  # se fija una sola vez


async def test_cambio_de_plazo_se_refleja_y_conserva_lo_previo(admin: Sesion) -> None:
    bdns = FalsaBdns()
    previa = await crear_convocatoria_en_bd(financiada_mrr=True, nivel_administracion="local")
    bdns.nueva(previa.codigo_bdns, fin="2026-10-30")
    bdns.grants[previa.codigo_bdns].pop("mrr")  # el detalle no dice nada del MRR
    bdns.grants[previa.codigo_bdns].pop("organo")  # ni del órgano
    await correr(bdns, admin)
    primero = await fila(previa.codigo_bdns)
    assert primero.id == previa.id  # un favorito apuntando a ella seguiría válido
    assert primero.financiada_mrr is True and primero.nivel_administracion == "local"
    bdns.grants[previa.codigo_bdns]["fechaFinSolicitud"] = "2026-11-15"
    run = await correr(bdns, admin)
    assert run is not None and run.actualizadas == 1
    assert (await fila(previa.codigo_bdns)).fecha_fin_solicitud == date(2026, 11, 15)


async def test_detalle_sin_mrr_en_fila_nueva_queda_en_false(admin: Sesion) -> None:
    bdns = FalsaBdns()
    codigo = bdns.nueva()
    bdns.grants[codigo].pop("mrr")
    run = await correr(bdns, admin)
    assert run is not None and run.errores == 0
    assert (await fila(codigo)).financiada_mrr is False


async def test_retirada_suave_y_reaparicion(admin: Sesion) -> None:
    bdns = FalsaBdns()
    codigo = bdns.nueva()
    await correr(bdns, admin)
    detalle = bdns.grants.pop(codigo)
    run = await correr(bdns, admin)
    assert run is not None and run.retiradas == 1
    assert (await fila(codigo)).removed_at is not None  # la fila sigue ahí
    bdns.grants[codigo] = detalle
    await correr(bdns, admin)
    assert (await fila(codigo)).removed_at is None


async def test_refresco_rotatorio_respeta_el_presupuesto(admin: Sesion) -> None:
    bdns = FalsaBdns()
    codigos = [bdns.nueva(registro=HOY - timedelta(days=60)) for _ in range(5)]
    await correr(bdns, admin, desde=HOY - timedelta(days=90))
    async with AsyncSessionLocal() as db:
        for i, codigo in enumerate(codigos):  # el más antiguo es el primero
            await db.execute(
                text("UPDATE convocatoria SET detalle_sync_at = :t WHERE codigo_bdns = :c"),
                {"t": AHORA - timedelta(days=10 - i), "c": codigo},
            )
        await db.commit()
    for detalle in bdns.grants.values():
        detalle["fechaFinSolicitud"] = "2027-03-01"
    bdns.detalles.clear()
    run = await correr(bdns, admin, presupuesto_refresco=2)
    assert run is not None and run.estado == "partial" and len(bdns.detalles) == 2
    assert sorted(bdns.detalles) == sorted(codigos[:2])
    await correr(bdns, admin, presupuesto_refresco=2)
    await correr(bdns, admin, presupuesto_refresco=2)  # ceil(5 / 2) = 3 pasadas
    for codigo in codigos:
        assert (await fila(codigo)).fecha_fin_solicitud == date(2027, 3, 1)


async def test_reanuda_desde_la_pagina_tras_el_breaker(admin: Sesion) -> None:
    bdns = FalsaBdns(tam_pagina=2)
    codigos = [bdns.nueva() for _ in range(6)]
    peticiones = bdns.__call__

    def cae_en_la_segunda(peticion: httpx.Request) -> httpx.Response:
        if peticion.url.path.endswith("/busqueda") and peticion.url.params["page"] == "1":
            return httpx.Response(503)
        return peticiones(peticion)

    cliente = cliente_de(bdns, umbral_breaker=1)
    cliente._cliente = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(cae_en_la_segunda), base_url="https://bdns.example/api"
    )
    async with cliente:
        fallida = await ejecutar(cliente, usuario_id=admin.id, prefijo="TST", ahora=lambda: AHORA, hoy=HOY)
    assert fallida is not None and fallida.estado == "failed"
    async with AsyncSessionLocal() as db:
        run = await db.get(SyncRun, fallida.run_id)
        assert run is not None and run.cursor["pagina"] == 1 and run.insertadas == 2
    bdns.listados.clear()
    run2 = await correr(bdns, admin)
    assert run2 is not None and run2.estado == "ok"
    assert [p["page"] for p in bdns.listados] == ["1", "2"]  # la página 0 no se pidió otra vez
    assert all([(await fila(c)).detalle_sync_at for c in codigos])


async def test_segundo_run_concurrente_no_hace_peticiones(admin: Sesion) -> None:
    bdns = FalsaBdns()
    bdns.nueva()
    async with engine.connect() as otra:
        await otra.execution_options(isolation_level="AUTOCOMMIT")
        await otra.execute(text("SELECT pg_advisory_lock(hashtext('sync_bdns'))"))
        try:
            resultado = await correr(bdns, admin)
        finally:
            await otra.execute(text("SELECT pg_advisory_unlock(hashtext('sync_bdns'))"))
    assert resultado is None and bdns.listados == [] and bdns.detalles == []


async def test_caja_de_tiempo_deja_run_parcial_y_el_siguiente_continua(admin: Sesion) -> None:
    bdns = FalsaBdns()
    codigos = [bdns.nueva(fin="2026-01-31") for _ in range(5)]  # cerradas: sin refresco
    bdns.avance_por_detalle = 10
    run = await correr(bdns, admin, max_minutos=25)
    assert run is not None and run.estado == "partial" and run.detalles == 3
    bdns.avance_minutos, bdns.avance_por_detalle = 0, 0
    run2 = await correr(bdns, admin, max_minutos=25)
    assert run2 is not None and run2.estado == "ok" and run2.detalles == 2
    assert all([(await fila(c)).detalle_sync_at for c in codigos])


async def test_backfill_por_trozos_mensuales_y_reanudacion(admin: Sesion) -> None:
    bdns = FalsaBdns()
    bdns.nueva(registro=date(2026, 8, 1))
    bdns.nueva(registro=date(2026, 10, 1))
    run = await correr(bdns, admin, modo="backfill", meses=3)
    assert run is not None and run.estado == "ok" and run.insertadas == 2
    assert [p["fechaDesde"] for p in bdns.listados] == ["26/07/2026", "26/08/2026", "26/09/2026", "26/10/2026"]
    assert bdns.listados[0]["fechaHasta"] == "25/08/2026"
    # Interrumpido en el segundo trozo: la segunda ejecución no repite el primero.
    bdns2 = FalsaBdns()
    bdns2.nueva(registro=date(2026, 8, 1))
    fallos = {"n": 0}

    def cae_en_el_segundo(peticion: httpx.Request) -> httpx.Response:
        if peticion.url.path.endswith("/busqueda") and peticion.url.params["fechaDesde"] == "26/08/2026":
            fallos["n"] += 1
            if fallos["n"] == 1:
                return httpx.Response(503)
        return bdns2(peticion)

    cliente = cliente_de(bdns2, umbral_breaker=1)
    cliente._cliente = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(cae_en_el_segundo), base_url="https://bdns.example/api"
    )
    async with cliente:
        fallida = await ejecutar(
            cliente, usuario_id=admin.id, prefijo="TST", modo="backfill", meses=3, ahora=lambda: AHORA, hoy=HOY
        )
    assert fallida is not None and fallida.estado == "failed"
    bdns2.listados.clear()
    await correr(bdns2, admin, modo="backfill", meses=3)
    assert [p["fechaDesde"] for p in bdns2.listados] == ["26/08/2026", "26/09/2026", "26/10/2026"]
