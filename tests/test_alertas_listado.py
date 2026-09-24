"""Listado y detalle de alertas (Hito 4).

Casi todas las alertas se insertan directamente en la BD y no por la API:
así el test controla `created_at` (orden) y puede simular volumen (N+1) sin
cientos de peticiones HTTP. El contrato de escritura ya lo prueba
`test_alertas.py`.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.database import AsyncSessionLocal, engine
from app.models import Alerta, AlertaOrgano, AlertaRegion
from tests.conftest import Sesion, crear_sesion_en_bd

ID_INEXISTENTE = 2**62


async def _insertar_alertas(
    usuario_id: int,
    n: int = 1,
    *,
    organos: Sequence[int] = (),
    regiones: Sequence[int] = (),
    **campos: Any,
) -> list[int]:
    """Inserta `n` alertas en una sola transacción y devuelve sus ids en orden
    de creación. Todas comparten created_at salvo que se pase uno explícito."""
    async with AsyncSessionLocal() as db:
        alertas = [
            Alerta(usuario_id=usuario_id, nombre=f"Alerta {i}", created_by=usuario_id, **campos) for i in range(n)
        ]
        db.add_all(alertas)
        await db.flush()
        for alerta in alertas:
            db.add_all(AlertaOrgano(alerta_id=alerta.id, organo_bdns_id=o) for o in organos)
            db.add_all(AlertaRegion(alerta_id=alerta.id, region_bdns_id=r) for r in regiones)
        await db.commit()
        return [alerta.id for alerta in alertas]


def _ids(respuesta: Any) -> list[int]:
    return [item["id"] for item in respuesta.json()["items"]]


@contextmanager
def _contar_consultas() -> Iterator[list[str]]:
    """Registra cada sentencia SQL que sale por el engine de la app.

    Los tests y la app comparten engine y proceso (ASGITransport), así que
    esto ve exactamente las consultas que hace el endpoint.
    """
    sentencias: list[str] = []

    def _registrar(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        sentencias.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _registrar)
    try:
        yield sentencias
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _registrar)


# --- Listado ------------------------------------------------------------


async def test_listado_solo_trae_las_alertas_propias(
    client_gestor: AsyncClient, gestor: Sesion, empresa: dict
) -> None:
    propias = await _insertar_alertas(gestor.id, 3)
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    await _insertar_alertas(companero.id, 2)

    respuesta = await client_gestor.get("/alertas")
    assert respuesta.status_code == 200
    assert respuesta.json()["total"] == 3
    assert sorted(_ids(respuesta)) == sorted(propias)


async def test_ni_el_admin_ve_las_alertas_de_otro(client_admin: AsyncClient, gestor: Sesion) -> None:
    await _insertar_alertas(gestor.id, 2)
    respuesta = await client_admin.get("/alertas")
    assert respuesta.json()["total"] == 0


async def test_listado_vacio(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.get("/alertas")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"items": [], "total": 0, "page": 1, "size": 20}


async def test_paginacion_sin_repetidos(client_gestor: AsyncClient, gestor: Sesion) -> None:
    creadas = await _insertar_alertas(gestor.id, 5)

    paginas = [await client_gestor.get("/alertas", params={"page": p, "size": 2}) for p in (1, 2, 3)]
    ids_por_pagina = [_ids(r) for r in paginas]
    assert [len(ids) for ids in ids_por_pagina] == [2, 2, 1]
    vistos = [i for ids in ids_por_pagina for i in ids]
    assert len(set(vistos)) == 5
    assert sorted(vistos) == sorted(creadas)
    assert all(r.json()["total"] == 5 for r in paginas)

    # Más allá de la última página: vacía, pero el total sigue siendo el real.
    fuera = await client_gestor.get("/alertas", params={"page": 4, "size": 2})
    assert fuera.json()["items"] == []
    assert fuera.json()["total"] == 5


@pytest.mark.parametrize("params", [{"size": 101}, {"size": 0}, {"page": 0}])
async def test_paginacion_fuera_de_limites(client_gestor: AsyncClient, params: dict) -> None:
    assert (await client_gestor.get("/alertas", params=params)).status_code == 422


async def test_la_mas_reciente_primero(client_gestor: AsyncClient, gestor: Sesion) -> None:
    """Manda created_at, no el id: la de id menor es aquí la más reciente."""
    (nueva,) = await _insertar_alertas(gestor.id, created_at=datetime(2026, 9, 2, tzinfo=UTC))
    (vieja,) = await _insertar_alertas(gestor.id, created_at=datetime(2026, 9, 1, tzinfo=UTC))
    assert nueva < vieja

    respuesta = await client_gestor.get("/alertas")
    assert _ids(respuesta) == [nueva, vieja]


async def test_empate_de_created_at_se_desempata_por_id(client_gestor: AsyncClient, gestor: Sesion) -> None:
    creadas = await _insertar_alertas(gestor.id, 3)  # misma transacción, mismo created_at
    assert _ids(await client_gestor.get("/alertas")) == sorted(creadas, reverse=True)


async def test_listado_incluye_los_filtros_de_cada_alerta(client_gestor: AsyncClient, gestor: Sesion) -> None:
    await _insertar_alertas(gestor.id, organos=[1500, 42], regiones=[9])
    await _insertar_alertas(gestor.id)

    items = (await client_gestor.get("/alertas")).json()["items"]
    assert sorted((i["organos"], i["regiones"]) for i in items) == [([], []), ([1500, 42], [9])]


# --- Filtros ------------------------------------------------------------


async def test_filtro_por_organo(client_gestor: AsyncClient, gestor: Sesion) -> None:
    (con_42,) = await _insertar_alertas(gestor.id, organos=[42, 7])
    await _insertar_alertas(gestor.id, organos=[43])

    assert _ids(await client_gestor.get("/alertas", params={"organo_id": 42})) == [con_42]
    assert _ids(await client_gestor.get("/alertas", params={"organo_id": 99})) == []


async def test_filtro_por_region(client_gestor: AsyncClient, gestor: Sesion) -> None:
    (con_9,) = await _insertar_alertas(gestor.id, regiones=[9])
    await _insertar_alertas(gestor.id, regiones=[4])

    respuesta = await client_gestor.get("/alertas", params={"region_id": 9})
    assert _ids(respuesta) == [con_9]
    assert respuesta.json()["total"] == 1


async def test_filtro_por_activa(client_gestor: AsyncClient, gestor: Sesion) -> None:
    (activa,) = await _insertar_alertas(gestor.id)
    (pausada,) = await _insertar_alertas(gestor.id, activa=False)

    assert _ids(await client_gestor.get("/alertas", params={"activa": "true"})) == [activa]
    assert _ids(await client_gestor.get("/alertas", params={"activa": "false"})) == [pausada]
    assert (await client_gestor.get("/alertas")).json()["total"] == 2  # sin filtro: todas


async def test_filtros_combinados(client_gestor: AsyncClient, gestor: Sesion) -> None:
    (buscada,) = await _insertar_alertas(gestor.id, organos=[42], regiones=[9])
    await _insertar_alertas(gestor.id, organos=[42], regiones=[4])
    await _insertar_alertas(gestor.id, organos=[42], regiones=[9], activa=False)

    respuesta = await client_gestor.get("/alertas", params={"organo_id": 42, "region_id": 9, "activa": "true"})
    assert _ids(respuesta) == [buscada]


async def test_el_filtro_no_cuela_alertas_ajenas(
    client_gestor: AsyncClient, gestor: Sesion, empresa: dict
) -> None:
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    await _insertar_alertas(companero.id, organos=[42])
    assert (await client_gestor.get("/alertas", params={"organo_id": 42})).json()["total"] == 0


@pytest.mark.parametrize("params", [{"organo_id": 0}, {"region_id": -1}, {"organo_id": "andalucia"}])
async def test_filtro_con_id_invalido(client_gestor: AsyncClient, params: dict) -> None:
    assert (await client_gestor.get("/alertas", params=params)).status_code == 422


# --- Rendimiento --------------------------------------------------------


async def test_sin_n_mas_1_con_200_alertas(client_gestor: AsyncClient, gestor: Sesion) -> None:
    """El número de consultas no depende del tamaño de la página."""
    await _insertar_alertas(gestor.id, 200, organos=[42, 1500], regiones=[9])

    with _contar_consultas() as una:
        pequena = await client_gestor.get("/alertas", params={"size": 1})
    with _contar_consultas() as cien:
        grande = await client_gestor.get("/alertas", params={"size": 100})

    assert pequena.status_code == grande.status_code == 200
    assert len(pequena.json()["items"]) == 1
    assert grande.json()["total"] == 200
    assert len(grande.json()["items"]) == 100
    assert all(i["organos"] == [42, 1500] and i["regiones"] == [9] for i in grande.json()["items"])
    # auth + count + página + órganos + regiones, sea cual sea el tamaño.
    assert len(cien) == len(una) <= 5, cien


# --- Detalle ------------------------------------------------------------


async def test_detalle_de_una_alerta_propia(client_gestor: AsyncClient, gestor: Sesion) -> None:
    creada = (
        await client_gestor.post(
            "/alertas",
            json={"nombre": "Kit Digital", "frecuencia": "semanal", "organos": [1500, 42], "regiones": [9]},
        )
    ).json()

    respuesta = await client_gestor.get(f"/alertas/{creada['id']}")
    assert respuesta.status_code == 200
    detalle = respuesta.json()
    assert detalle == creada  # lo mismo que devolvió el POST
    assert detalle["usuario_id"] == gestor.id
    assert detalle["organos"] == [1500, 42]
    assert detalle["regiones"] == [9]
    assert detalle["frecuencia"] == "semanal"


async def test_detalle_de_una_alerta_inexistente(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.get(f"/alertas/{ID_INEXISTENTE}")
    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "Alerta no encontrada."


async def test_detalle_de_una_alerta_ajena_da_404(
    client_gestor: AsyncClient, client_admin: AsyncClient, empresa: dict
) -> None:
    """404 y no 403, con el mismo mensaje que un id inexistente: no se
    confirma que la alerta exista."""
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    (ajena,) = await _insertar_alertas(companero.id)

    del_gestor = await client_gestor.get(f"/alertas/{ajena}")
    del_admin = await client_admin.get(f"/alertas/{ajena}")
    assert del_gestor.status_code == del_admin.status_code == 404
    assert del_gestor.json() == (await client_gestor.get(f"/alertas/{ID_INEXISTENTE}")).json()


async def test_lectura_exige_token(client: AsyncClient) -> None:
    assert (await client.get("/alertas")).status_code == 401
    assert (await client.get("/alertas/1")).status_code == 401
