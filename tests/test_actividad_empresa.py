"""Company activity API (PR B1, `empresa-actividad-ia`): catalog, create/edit rules, pending flag, /auth/me."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Empresa
from tests.conftest import Sesion, crear_empresa_en_bd, crear_sesion_en_bd, nif_de_prueba


def _alta(**extra) -> dict:
    return {"razon_social": "Actividad SL", "nif": nif_de_prueba(), "actividad_codigo": "J", **extra}


# --- catalog -------------------------------------------------------------------------------


async def test_catalog_lists_23_options_in_order(client_usuario: AsyncClient) -> None:
    r = await client_usuario.get("/catalogos/actividades")
    assert r.status_code == 200
    cuerpo = r.json()
    assert len(cuerpo) == 23
    assert [o["orden"] for o in cuerpo] == list(range(1, 24))
    assert cuerpo[0] == {"codigo": "A", "descripcion": "Agricultura, ganadería, silvicultura y pesca", "orden": 1}
    assert cuerpo[-1]["codigo"] == "OTRA"
    assert set(cuerpo[0]) == {"codigo", "descripcion", "orden"}  # no division ranges leaked


async def test_catalog_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/catalogos/actividades")).status_code == 401


# --- create --------------------------------------------------------------------------------


async def test_create_with_section(client_admin: AsyncClient) -> None:
    r = await client_admin.post("/empresas", json=_alta())
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["actividad_codigo"], c["actividad_otra"]) == ("J", None)
    assert (c["actividad_pendiente"], c["ofrecer_ia"]) == (False, False)


async def test_create_otra_with_text_flags_ai_offer(client_admin: AsyncClient) -> None:
    r = await client_admin.post("/empresas", json=_alta(actividad_codigo="OTRA", actividad_otra="  Apicultura  "))
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["actividad_otra"], c["ofrecer_ia"], c["actividad_pendiente"]) == ("Apicultura", True, False)


async def test_create_without_activity_is_422(client_admin: AsyncClient) -> None:
    cuerpo = _alta()
    del cuerpo["actividad_codigo"]
    r = await client_admin.post("/empresas", json=cuerpo)
    assert r.status_code == 422
    assert "actividad_codigo" in r.text


@pytest.mark.parametrize("otra", [None, "", "   "])
async def test_create_otra_without_text_is_422(client_admin: AsyncClient, otra: str | None) -> None:
    r = await client_admin.post("/empresas", json=_alta(actividad_codigo="OTRA", actividad_otra=otra))
    assert r.status_code == 422


async def test_create_unknown_section_is_422(client_admin: AsyncClient) -> None:
    assert (await client_admin.post("/empresas", json=_alta(actividad_codigo="Z"))).status_code == 422


async def test_create_otra_text_over_200_is_422(client_admin: AsyncClient) -> None:
    r = await client_admin.post("/empresas", json=_alta(actividad_codigo="OTRA", actividad_otra="x" * 201))
    assert r.status_code == 422


async def test_create_section_discards_stray_otra_text(client_admin: AsyncClient) -> None:
    r = await client_admin.post("/empresas", json=_alta(actividad_codigo="C", actividad_otra="ruido"))
    assert r.status_code == 201
    assert r.json()["actividad_otra"] is None


# --- edit ----------------------------------------------------------------------------------


async def test_gestor_edits_own_company_activity(client_gestor: AsyncClient, empresa: dict) -> None:
    r = await client_gestor.patch(f"/empresas/{empresa['id']}", json={"actividad_codigo": "F"})
    assert r.status_code == 200, r.text
    assert (r.json()["actividad_codigo"], r.json()["actividad_pendiente"]) == ("F", False)


async def test_patch_to_otra_requires_text_and_leaving_otra_clears_it(
    client_gestor: AsyncClient, empresa: dict
) -> None:
    url = f"/empresas/{empresa['id']}"
    assert (await client_gestor.patch(url, json={"actividad_codigo": "OTRA"})).status_code == 422
    ok = await client_gestor.patch(url, json={"actividad_codigo": "OTRA", "actividad_otra": "Apicultura"})
    assert ok.status_code == 200 and ok.json()["ofrecer_ia"] is True
    # Text-only edit while already OTRA is valid.
    ok = await client_gestor.patch(url, json={"actividad_otra": "Cría de abejas"})
    assert ok.json()["actividad_otra"] == "Cría de abejas"
    # Blanking the text while OTRA is not.
    assert (await client_gestor.patch(url, json={"actividad_otra": "  "})).status_code == 422
    # Leaving OTRA clears the free text.
    salida = await client_gestor.patch(url, json={"actividad_codigo": "A"})
    assert (salida.json()["actividad_otra"], salida.json()["ofrecer_ia"]) == (None, False)


async def test_patch_cannot_null_the_activity(client_gestor: AsyncClient, empresa: dict) -> None:
    r = await client_gestor.patch(f"/empresas/{empresa['id']}", json={"actividad_codigo": None})
    assert r.status_code == 422


async def test_patch_without_activity_keeps_it(client_gestor: AsyncClient, empresa: dict) -> None:
    url = f"/empresas/{empresa['id']}"
    await client_gestor.patch(url, json={"actividad_codigo": "K"})
    r = await client_gestor.patch(url, json={"descripcion": "otra cosa"})
    assert r.json()["actividad_codigo"] == "K"


async def test_regular_user_cannot_edit_activity(client_usuario: AsyncClient, empresa: dict) -> None:
    r = await client_usuario.patch(f"/empresas/{empresa['id']}", json={"actividad_codigo": "F"})
    assert r.status_code == 403


async def test_gestor_cannot_touch_another_company(client_gestor: AsyncClient, otra_empresa: dict) -> None:
    r = await client_gestor.patch(f"/empresas/{otra_empresa['id']}", json={"actividad_codigo": "F"})
    assert r.status_code == 404  # tenant isolation: indistinguishable from a missing id
    async with AsyncSessionLocal() as db:
        otra = await db.get(Empresa, otra_empresa["id"])
        assert otra is not None and otra.actividad_codigo is None


# --- legacy / pending ----------------------------------------------------------------------


async def test_legacy_company_is_pending(client_gestor: AsyncClient, empresa: dict) -> None:
    c = (await client_gestor.get(f"/empresas/{empresa['id']}")).json()
    assert (c["actividad_codigo"], c["actividad_pendiente"], c["ofrecer_ia"]) == (None, True, False)


# --- /auth/me ------------------------------------------------------------------------------


async def test_me_exposes_company_flags(client_gestor: AsyncClient, gestor: Sesion) -> None:
    me = (await client_gestor.get("/auth/me")).json()
    assert me["empresa"] == {"id": gestor.empresa_id, "actividad_pendiente": True, "ofrecer_ia": False}
    cambio = {"actividad_codigo": "OTRA", "actividad_otra": "X"}
    await client_gestor.patch(f"/empresas/{gestor.empresa_id}", json=cambio)
    me = (await client_gestor.get("/auth/me")).json()
    assert me["empresa"] == {"id": gestor.empresa_id, "actividad_pendiente": False, "ofrecer_ia": True}
    assert me["email"] == gestor.email  # still the user payload


async def test_me_of_a_user_in_a_completed_company() -> None:
    e = await crear_empresa_en_bd()
    async with AsyncSessionLocal() as db:
        fila = (await db.execute(select(Empresa).where(Empresa.id == e["id"]))).scalar_one()
        fila.actividad_codigo = "C"
        await db.commit()
    s = await crear_sesion_en_bd(e["id"], "usuario")
    from httpx import ASGITransport

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=s.headers) as c:
        me = (await c.get("/auth/me")).json()
    assert me["empresa"]["actividad_pendiente"] is False
