"""Discards (N4): hide a grant from the radar and the news, personal to each user, reversible."""

from httpx import AsyncClient
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import Descarte
from tests.conftest import Sesion, codigo_bdns_de_prueba
from tests.test_radar import marca, perfilar, sembrar_convocatoria, sembrar_regiones


async def _contar(usuario_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(Descarte).where(Descarte.usuario_id == usuario_id)) or 0


async def _en_radar(cliente: AsyncClient) -> set[str]:
    r = await cliente.get("/radar", params={"min": 100, "limit": 200})
    assert r.status_code == 200, r.text
    return {i["codigo_bdns"] for i in r.json()["items"]}


async def test_requieren_autenticacion(client: AsyncClient) -> None:
    assert (await client.post("/descartes", json={"codigo_bdns": "X"})).status_code == 401
    assert (await client.delete("/descartes/X")).status_code == 401


async def test_descartar_crea_y_es_idempotente(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    codigo = await sembrar_convocatoria()
    r = await client_usuario.post("/descartes", json={"codigo_bdns": codigo})
    assert r.status_code == 201, r.text
    assert set(r.json()) == {"codigo_bdns", "descartada_at"} and r.json()["codigo_bdns"] == codigo
    de_nuevo = await client_usuario.post("/descartes", json={"codigo_bdns": codigo})
    assert de_nuevo.status_code == 200
    assert de_nuevo.json() == r.json()  # the moment of the first discard, not of the repeat
    assert await _contar(usuario_raso.id) == 1


async def test_descartar_valida_la_entrada(client_usuario: AsyncClient) -> None:
    r = await client_usuario.post("/descartes", json={"codigo_bdns": codigo_bdns_de_prueba()})
    assert r.status_code == 404
    for malo in ({}, {"codigo_bdns": ""}, {"codigo_bdns": "x" * 31}, {"codigo_bdns": 5}):
        assert (await client_usuario.post("/descartes", json=malo)).status_code == 422


async def test_recuperar_y_el_segundo_intento_da_404(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    codigo = await sembrar_convocatoria()
    await client_usuario.post("/descartes", json={"codigo_bdns": codigo})
    assert (await client_usuario.delete(f"/descartes/{codigo}")).status_code == 204
    assert await _contar(usuario_raso.id) == 0
    assert (await client_usuario.delete(f"/descartes/{codigo}")).status_code == 404
    assert (await client_usuario.delete("/descartes/NOEXISTE")).status_code == 404


async def test_el_radar_la_oculta_y_al_recuperarla_vuelve(
    client_usuario: AsyncClient, empresa: dict, usuario_raso: Sesion
) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    codigo = await sembrar_convocatoria()
    assert codigo in await _en_radar(client_usuario)
    await client_usuario.post("/descartes", json={"codigo_bdns": codigo})
    assert codigo not in await _en_radar(client_usuario)
    await client_usuario.delete(f"/descartes/{codigo}")
    assert codigo in await _en_radar(client_usuario)


async def test_el_descarte_es_personal_y_no_afecta_a_un_companero(
    client_usuario: AsyncClient, client_gestor: AsyncClient, empresa: dict, usuario_raso: Sesion, gestor: Sesion
) -> None:
    await sembrar_regiones()
    await perfilar(empresa["id"], usuario_raso.id)
    codigo = await sembrar_convocatoria()
    await client_usuario.post("/descartes", json={"codigo_bdns": codigo})
    assert codigo not in await _en_radar(client_usuario)
    assert codigo in await _en_radar(client_gestor)  # same company, other person
    assert (await client_gestor.delete(f"/descartes/{codigo}")).status_code == 404  # not his to undo
    assert codigo not in await _en_radar(client_usuario)
    assert await _contar(gestor.id) == 0


async def test_cualquier_rol_puede_descartar(client_admin: AsyncClient, client_gestor: AsyncClient) -> None:
    codigo = await sembrar_convocatoria()
    for cliente in (client_admin, client_gestor):
        assert (await cliente.post("/descartes", json={"codigo_bdns": codigo})).status_code == 201


async def test_el_buscador_sigue_mostrando_la_descartada(client_usuario: AsyncClient) -> None:
    m = marca()
    codigo = await sembrar_convocatoria(titulo=f"Ayudas {m}")
    await client_usuario.post("/descartes", json={"codigo_bdns": codigo})
    r = await client_usuario.get("/convocatorias", params={"q": m})
    assert [i["codigo_bdns"] for i in r.json()["items"]] == [codigo]
    ordenado = await client_usuario.get("/convocatorias", params={"q": m, "orden": "afinidad"})
    assert [i["codigo_bdns"] for i in ordenado.json()["items"]] == [codigo]
