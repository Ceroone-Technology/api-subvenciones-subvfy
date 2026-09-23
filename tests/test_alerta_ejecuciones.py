"""Historial de ejecuciones de una alerta (Hito 4).

Las ejecuciones las escribirá el motor de alertas, que todavía no existe, así
que aquí se insertan directamente en la BD. De paso, es la única forma de
controlar `fecha_ejecucion_at` y comprobar el orden sin depender de los ids.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.database import AsyncSessionLocal
from app.models import Alerta, AlertaEjecucion, AlertaEjecucionConvocatoria, Convocatoria
from tests.conftest import Sesion, codigo_bdns_de_prueba, crear_sesion_en_bd

ID_INEXISTENTE = 2**62


async def _crear_alerta(usuario_id: int, nombre: str = "Alerta con historial") -> int:
    async with AsyncSessionLocal() as db:
        alerta = Alerta(usuario_id=usuario_id, nombre=nombre, created_by=usuario_id)
        db.add(alerta)
        await db.commit()
        return alerta.id


async def _crear_ejecucion(
    alerta_id: int,
    *,
    estado_envio: str = "enviado",
    fecha: datetime | None = None,
    convocatorias: int = 0,
    detalle_error: str | None = None,
) -> tuple[int, list[str]]:
    """Inserta una ejecución y, si se piden, sus convocatorias detectadas
    (que van a la caché local). Devuelve el id y los códigos BDNS creados."""
    async with AsyncSessionLocal() as db:
        ejecucion = AlertaEjecucion(
            alerta_id=alerta_id,
            estado_envio=estado_envio,
            convocatorias_encontradas=convocatorias,
            detalle_error=detalle_error,
            **({"fecha_ejecucion_at": fecha} if fecha else {}),
        )
        db.add(ejecucion)
        await db.flush()

        codigos = []
        for i in range(convocatorias):
            codigo = codigo_bdns_de_prueba()
            convocatoria = Convocatoria(codigo_bdns=codigo, titulo=f"Convocatoria detectada {i}")
            db.add(convocatoria)
            await db.flush()
            db.add(
                AlertaEjecucionConvocatoria(
                    alerta_ejecucion_id=ejecucion.id, convocatoria_id=convocatoria.id
                )
            )
            codigos.append(codigo)
        await db.commit()
        return ejecucion.id, codigos


def _ids(respuesta: Any) -> list[int]:
    return [item["id"] for item in respuesta.json()["items"]]


# --- Listado ------------------------------------------------------------


async def test_historial_de_una_alerta(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    otra = await _crear_alerta(gestor.id, "Otra alerta")
    mias = [(await _crear_ejecucion(alerta))[0] for _ in range(2)]
    await _crear_ejecucion(otra)

    respuesta = await client_gestor.get(f"/alertas/{alerta}/ejecuciones")
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 2
    assert sorted(_ids(respuesta)) == sorted(mias)
    assert all(item["alerta_id"] == alerta for item in cuerpo["items"])


async def test_historial_vacio(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    respuesta = await client_gestor.get(f"/alertas/{alerta}/ejecuciones")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"items": [], "total": 0, "page": 1, "size": 20}


async def test_resumen_de_una_ejecucion_con_error(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    await _crear_ejecucion(alerta, estado_envio="error", detalle_error="SMTP timeout")

    item = (await client_gestor.get(f"/alertas/{alerta}/ejecuciones")).json()["items"][0]
    assert item["estado_envio"] == "error"
    assert item["detalle_error"] == "SMTP timeout"
    assert item["convocatorias_encontradas"] == 0
    # El resumen no arrastra las convocatorias: eso es el detalle.
    assert "convocatorias" not in item


async def test_paginacion_del_historial(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    creadas = [(await _crear_ejecucion(alerta))[0] for _ in range(5)]

    paginas = [
        await client_gestor.get(f"/alertas/{alerta}/ejecuciones", params={"page": p, "size": 2})
        for p in (1, 2, 3)
    ]
    vistos = [i for pagina in paginas for i in _ids(pagina)]
    assert [len(_ids(p)) for p in paginas] == [2, 2, 1]
    assert sorted(vistos) == sorted(creadas)
    assert len(set(vistos)) == 5
    assert all(p.json()["total"] == 5 for p in paginas)


@pytest.mark.parametrize("params", [{"size": 101}, {"page": 0}])
async def test_paginacion_fuera_de_limites(client_gestor: AsyncClient, gestor: Sesion, params: dict) -> None:
    alerta = await _crear_alerta(gestor.id)
    assert (await client_gestor.get(f"/alertas/{alerta}/ejecuciones", params=params)).status_code == 422


async def test_la_ejecucion_mas_reciente_primero(client_gestor: AsyncClient, gestor: Sesion) -> None:
    """Manda fecha_ejecucion_at: la insertada antes es aquí la más reciente."""
    alerta = await _crear_alerta(gestor.id)
    nueva, _ = await _crear_ejecucion(alerta, fecha=datetime(2026, 9, 20, tzinfo=UTC))
    vieja, _ = await _crear_ejecucion(alerta, fecha=datetime(2026, 9, 1, tzinfo=UTC))
    assert nueva < vieja

    assert _ids(await client_gestor.get(f"/alertas/{alerta}/ejecuciones")) == [nueva, vieja]


async def test_empate_de_fecha_se_desempata_por_id(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    misma_fecha = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    creadas = [(await _crear_ejecucion(alerta, fecha=misma_fecha))[0] for _ in range(3)]

    assert _ids(await client_gestor.get(f"/alertas/{alerta}/ejecuciones")) == sorted(creadas, reverse=True)


# --- Filtro -------------------------------------------------------------


async def test_filtro_por_estado_de_envio(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    con_error, _ = await _crear_ejecucion(alerta, estado_envio="error", detalle_error="fallo")
    await _crear_ejecucion(alerta, estado_envio="enviado")
    await _crear_ejecucion(alerta, estado_envio="sin_novedades")

    solo_error = await client_gestor.get(f"/alertas/{alerta}/ejecuciones", params={"estado_envio": "error"})
    assert _ids(solo_error) == [con_error]
    assert solo_error.json()["total"] == 1
    assert (await client_gestor.get(f"/alertas/{alerta}/ejecuciones")).json()["total"] == 3


async def test_filtro_con_estado_invalido(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    respuesta = await client_gestor.get(
        f"/alertas/{alerta}/ejecuciones", params={"estado_envio": "rebotado"}
    )
    assert respuesta.status_code == 422


# --- Detalle ------------------------------------------------------------


async def test_detalle_trae_las_convocatorias_detectadas(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    ejecucion, codigos = await _crear_ejecucion(alerta, convocatorias=2)

    respuesta = await client_gestor.get(f"/alertas/{alerta}/ejecuciones/{ejecucion}")
    assert respuesta.status_code == 200, respuesta.text
    detalle = respuesta.json()
    assert detalle["id"] == ejecucion
    assert detalle["convocatorias_encontradas"] == 2
    # En el orden en que se registraron, y con la ficha que hay en la caché.
    assert [c["codigo_bdns"] for c in detalle["convocatorias"]] == codigos
    assert detalle["convocatorias"][0]["titulo"] == "Convocatoria detectada 0"


async def test_detalle_sin_novedades_no_trae_convocatorias(
    client_gestor: AsyncClient, gestor: Sesion
) -> None:
    alerta = await _crear_alerta(gestor.id)
    ejecucion, _ = await _crear_ejecucion(alerta, estado_envio="sin_novedades")

    detalle = (await client_gestor.get(f"/alertas/{alerta}/ejecuciones/{ejecucion}")).json()
    assert detalle["convocatorias"] == []
    assert detalle["estado_envio"] == "sin_novedades"


async def test_detalle_de_una_ejecucion_inexistente(client_gestor: AsyncClient, gestor: Sesion) -> None:
    alerta = await _crear_alerta(gestor.id)
    respuesta = await client_gestor.get(f"/alertas/{alerta}/ejecuciones/{ID_INEXISTENTE}")
    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "Ejecución no encontrada."


async def test_una_ejecucion_no_se_lee_desde_otra_alerta(client_gestor: AsyncClient, gestor: Sesion) -> None:
    """Las dos alertas son del mismo usuario: lo que falla aquí es el alerta_id."""
    alerta = await _crear_alerta(gestor.id)
    otra = await _crear_alerta(gestor.id, "Otra alerta")
    ejecucion, _ = await _crear_ejecucion(alerta)

    assert (await client_gestor.get(f"/alertas/{otra}/ejecuciones/{ejecucion}")).status_code == 404
    assert (await client_gestor.get(f"/alertas/{alerta}/ejecuciones/{ejecucion}")).status_code == 200


# --- Aislamiento --------------------------------------------------------


async def test_alerta_inexistente(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.get(f"/alertas/{ID_INEXISTENTE}/ejecuciones")
    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "Alerta no encontrada."


async def test_no_se_ve_el_historial_de_una_alerta_ajena(
    client_gestor: AsyncClient, client_admin: AsyncClient, empresa: dict
) -> None:
    """404 como si la alerta no existiera, admin incluido."""
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    ajena = await _crear_alerta(companero.id)
    ejecucion, _ = await _crear_ejecucion(ajena)

    for cliente in (client_gestor, client_admin):
        listado = await cliente.get(f"/alertas/{ajena}/ejecuciones")
        detalle = await cliente.get(f"/alertas/{ajena}/ejecuciones/{ejecucion}")
        assert listado.status_code == detalle.status_code == 404
        assert listado.json()["detail"] == "Alerta no encontrada."


async def test_el_historial_exige_token(client: AsyncClient) -> None:
    assert (await client.get("/alertas/1/ejecuciones")).status_code == 401
    assert (await client.get("/alertas/1/ejecuciones/1")).status_code == 401
