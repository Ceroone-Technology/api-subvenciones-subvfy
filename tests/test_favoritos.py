"""Favoritos (Hito 3, Funcionalidad 1).

Es la primera funcionalidad de negocio sobre la pila completa: token,
permisos, caché de convocatoria y join. Los tests van de punta a punta a
propósito.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Convocatoria
from tests.conftest import Sesion, codigo_bdns_de_prueba, crear_sesion_en_bd


def _convocatoria(codigo: str, **extra) -> dict:
    return {"codigo_bdns": codigo, "titulo": "Ayudas a la digitalización de pymes", **extra}


@pytest.mark.asyncio
async def test_marcar_favorito_crea_la_convocatoria_en_cache(
    client_gestor: AsyncClient, gestor: Sesion
) -> None:
    codigo = codigo_bdns_de_prueba()
    respuesta = await client_gestor.post(
        "/favoritos",
        json={
            "convocatoria": _convocatoria(
                codigo,
                nivel_administracion="estado",
                organo_convocante="Ministerio de Industria",
                url_portal_oficial="https://www.infosubvenciones.es/",
                financiada_mrr=True,
            ),
            "nota": "Encaja con el proyecto de facturación",
        },
    )
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["nota"] == "Encaja con el proyecto de facturación"
    assert cuerpo["convocatoria"]["codigo_bdns"] == codigo
    assert cuerpo["convocatoria"]["organo_convocante"] == "Ministerio de Industria"
    assert cuerpo["convocatoria"]["financiada_mrr"] is True

    # La convocatoria no existía: marcarla como favorita es lo que siembra
    # la caché local que referencia la FK.
    async with AsyncSessionLocal() as db:
        cacheada = (
            await db.execute(select(Convocatoria).where(Convocatoria.codigo_bdns == codigo))
        ).scalar_one()
    assert cacheada.titulo == "Ayudas a la digitalización de pymes"
    assert cacheada.created_by == gestor.id


@pytest.mark.asyncio
async def test_marcar_favorito_reutiliza_la_convocatoria_ya_cacheada(
    client_gestor: AsyncClient, empresa: dict
) -> None:
    """Dos usuarios marcando la misma convocatoria no duplican la caché."""
    codigo = codigo_bdns_de_prueba()
    otro = await crear_sesion_en_bd(empresa["id"], "usuario")

    primera = await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(codigo)})
    segunda = await client_gestor.post(
        "/favoritos", json={"convocatoria": _convocatoria(codigo)}, headers=otro.headers
    )
    assert primera.status_code == 201
    assert segunda.status_code == 201
    assert primera.json()["convocatoria"]["id"] == segunda.json()["convocatoria"]["id"]

    async with AsyncSessionLocal() as db:
        cuantas = len(
            (
                await db.execute(select(Convocatoria.id).where(Convocatoria.codigo_bdns == codigo))
            ).all()
        )
    assert cuantas == 1


@pytest.mark.asyncio
async def test_marcar_dos_veces_la_misma_convocatoria(client_gestor: AsyncClient) -> None:
    codigo = codigo_bdns_de_prueba()
    assert (
        await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(codigo)})
    ).status_code == 201
    repetido = await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(codigo)})
    assert repetido.status_code == 409
    assert codigo in repetido.json()["detail"]


@pytest.mark.asyncio
async def test_un_guardado_parcial_no_borra_datos_ya_cacheados(client_gestor: AsyncClient) -> None:
    """Marcar desde el listado de resultados (ficha incompleta) no puede
    machacar lo que ya se había sincronizado de la ficha completa."""
    codigo = codigo_bdns_de_prueba()
    await client_gestor.post(
        "/favoritos",
        json={"convocatoria": _convocatoria(codigo, organo_convocante="Ministerio de Industria")},
    )
    await client_gestor.delete(f"/favoritos/{codigo}")

    # Segunda vez, con la ficha mínima que trae el listado.
    respuesta = await client_gestor.post(
        "/favoritos", json={"convocatoria": {"codigo_bdns": codigo, "titulo": "Título abreviado"}}
    )
    assert respuesta.status_code == 201
    convocatoria = respuesta.json()["convocatoria"]
    assert convocatoria["titulo"] == "Título abreviado"  # sí se actualiza lo que viene
    assert convocatoria["organo_convocante"] == "Ministerio de Industria"  # no se pierde lo que no


@pytest.mark.asyncio
async def test_listado_trae_la_convocatoria_resuelta(client_gestor: AsyncClient) -> None:
    codigo = codigo_bdns_de_prueba()
    await client_gestor.post(
        "/favoritos",
        json={"convocatoria": _convocatoria(codigo, administracion="Estado"), "nota": "Revisar"},
    )

    respuesta = await client_gestor.get("/favoritos")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    item = cuerpo["items"][0]
    # El join evita que el frontend tenga que volver a la BDNS para pintar
    # la lista.
    assert item["convocatoria"]["codigo_bdns"] == codigo
    assert item["convocatoria"]["administracion"] == "Estado"
    assert item["nota"] == "Revisar"


@pytest.mark.asyncio
async def test_listado_ordena_por_el_ultimo_marcado(client_gestor: AsyncClient) -> None:
    primero = codigo_bdns_de_prueba()
    segundo = codigo_bdns_de_prueba()
    await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(primero)})
    await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(segundo)})

    respuesta = await client_gestor.get("/favoritos")
    codigos = [i["convocatoria"]["codigo_bdns"] for i in respuesta.json()["items"]]
    assert codigos == [segundo, primero]


@pytest.mark.asyncio
async def test_listado_busca_por_titulo_y_codigo(client_gestor: AsyncClient) -> None:
    buscado = codigo_bdns_de_prueba()
    otro = codigo_bdns_de_prueba()
    await client_gestor.post(
        "/favoritos", json={"convocatoria": _convocatoria(buscado, titulo="Kit Digital 2026")}
    )
    await client_gestor.post(
        "/favoritos", json={"convocatoria": _convocatoria(otro, titulo="Ayudas a la exportación")}
    )

    por_titulo = await client_gestor.get("/favoritos", params={"q": "kit digital"})
    assert [i["convocatoria"]["codigo_bdns"] for i in por_titulo.json()["items"]] == [buscado]

    por_codigo = await client_gestor.get("/favoritos", params={"q": buscado})
    assert por_codigo.json()["total"] == 1


@pytest.mark.asyncio
async def test_los_favoritos_son_personales(
    client_gestor: AsyncClient, client_admin: AsyncClient, empresa: dict
) -> None:
    """Ni un compañero de empresa ni un admin ven los favoritos de otro: la
    tabla cuelga del usuario, no de la empresa."""
    codigo = codigo_bdns_de_prueba()
    await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(codigo)})

    companero = await crear_sesion_en_bd(empresa["id"], "usuario")
    del_companero = await client_gestor.get("/favoritos", headers=companero.headers)
    assert del_companero.json()["total"] == 0

    del_admin = await client_admin.get("/favoritos")
    assert del_admin.json()["total"] == 0
    assert (await client_admin.get(f"/favoritos/{codigo}")).status_code == 404


@pytest.mark.asyncio
async def test_consultar_un_favorito_concreto(client_gestor: AsyncClient) -> None:
    codigo = codigo_bdns_de_prueba()
    await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(codigo)})

    marcado = await client_gestor.get(f"/favoritos/{codigo}")
    assert marcado.status_code == 200
    # El 404 es la respuesta a "¿la tengo marcada?" desde la ficha de detalle.
    assert (await client_gestor.get(f"/favoritos/{codigo_bdns_de_prueba()}")).status_code == 404


@pytest.mark.asyncio
async def test_editar_la_nota(client_gestor: AsyncClient) -> None:
    codigo = codigo_bdns_de_prueba()
    await client_gestor.post(
        "/favoritos", json={"convocatoria": _convocatoria(codigo), "nota": "Primera idea"}
    )

    respuesta = await client_gestor.patch(
        f"/favoritos/{codigo}", json={"nota": "Plazo hasta el 30 de noviembre"}
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["nota"] == "Plazo hasta el 30 de noviembre"

    # Y se puede vaciar.
    vaciada = await client_gestor.patch(f"/favoritos/{codigo}", json={"nota": None})
    assert vaciada.json()["nota"] is None


@pytest.mark.asyncio
async def test_no_se_edita_la_nota_de_otro(client_gestor: AsyncClient, empresa: dict) -> None:
    codigo = codigo_bdns_de_prueba()
    await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(codigo)})

    otro = await crear_sesion_en_bd(empresa["id"], "usuario")
    respuesta = await client_gestor.patch(
        f"/favoritos/{codigo}", json={"nota": "Mía ahora"}, headers=otro.headers
    )
    assert respuesta.status_code == 404


@pytest.mark.asyncio
async def test_quitar_favorito_conserva_la_convocatoria(client_gestor: AsyncClient) -> None:
    codigo = codigo_bdns_de_prueba()
    await client_gestor.post("/favoritos", json={"convocatoria": _convocatoria(codigo)})

    assert (await client_gestor.delete(f"/favoritos/{codigo}")).status_code == 204
    assert (await client_gestor.get(f"/favoritos/{codigo}")).status_code == 404

    # La caché de convocatorias no se toca: la comparten alertas y análisis IA.
    async with AsyncSessionLocal() as db:
        sigue = (
            await db.execute(select(Convocatoria.id).where(Convocatoria.codigo_bdns == codigo))
        ).first()
    assert sigue is not None


@pytest.mark.asyncio
async def test_quitar_un_favorito_que_no_tienes(client_gestor: AsyncClient) -> None:
    assert (await client_gestor.delete(f"/favoritos/{codigo_bdns_de_prueba()}")).status_code == 404


@pytest.mark.asyncio
async def test_nivel_administracion_invalido(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.post(
        "/favoritos",
        json={"convocatoria": _convocatoria(codigo_bdns_de_prueba(), nivel_administracion="galactico")},
    )
    assert respuesta.status_code == 422


@pytest.mark.asyncio
async def test_favoritos_exige_token(client: AsyncClient) -> None:
    assert (await client.get("/favoritos")).status_code == 401
    assert (await client.post("/favoritos", json={})).status_code == 401
