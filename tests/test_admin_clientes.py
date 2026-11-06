"""Admin client panel: users across companies, role/status changes and the company file with its read audit log.

The database is shared with real accounts, so every list is filtered by the test company or a unique email word.
The "last active admin" guard is not exercised end to end: it would need every real admin out of the way.
"""

from httpx import AsyncClient
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import AdminLecturaCliente, Alerta, Favorito, Seguimiento
from tests.conftest import (
    Sesion,
    crear_alerta_en_bd,
    crear_convocatoria_en_bd,
    crear_sesion_en_bd,
)


async def _lecturas(empresa_id: int, admin_id: int | None = None) -> list[AdminLecturaCliente]:
    async with AsyncSessionLocal() as db:
        consulta = select(AdminLecturaCliente).where(AdminLecturaCliente.empresa_id == empresa_id)
        if admin_id is not None:
            consulta = consulta.where(AdminLecturaCliente.admin_usuario_id == admin_id)
        return list(await db.scalars(consulta.order_by(AdminLecturaCliente.id)))


async def test_only_admin_can_use_the_panel(
    client_gestor: AsyncClient, client_usuario: AsyncClient, client: AsyncClient, gestor: Sesion
) -> None:
    for ruta in ("/admin/usuarios", f"/admin/empresas/{gestor.empresa_id}"):
        assert (await client.get(ruta)).status_code == 401
        assert (await client_gestor.get(ruta)).status_code == 403
        assert (await client_usuario.get(ruta)).status_code == 403
    assert (await client_gestor.patch(f"/admin/usuarios/{gestor.id}", json={"estado": "inactivo"})).status_code == 403


async def test_lists_users_of_every_company_with_filters(
    client_admin: AsyncClient, gestor: Sesion, empresa: dict, otra_empresa: dict
) -> None:
    usuario = await crear_sesion_en_bd(empresa["id"], "usuario", estado="bloqueado")
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "gestor")

    r = await client_admin.get("/admin/usuarios", params={"empresa_id": empresa["id"]})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert {u["id"] for u in cuerpo["items"]} == {gestor.id, usuario.id}
    assert cuerpo["items"][0]["empresa"] == empresa["razon_social"]
    assert "password_hash" not in r.text and "password" not in cuerpo["items"][0]

    solo_bloqueados = await client_admin.get(
        "/admin/usuarios", params={"empresa_id": empresa["id"], "estado": "bloqueado"}
    )
    assert [u["id"] for u in solo_bloqueados.json()["items"]] == [usuario.id]
    solo_gestores = await client_admin.get("/admin/usuarios", params={"empresa_id": empresa["id"], "rol": "gestor"})
    assert [u["id"] for u in solo_gestores.json()["items"]] == [gestor.id]
    por_texto = await client_admin.get("/admin/usuarios", params={"q": ajeno.email.split("@")[0]})
    assert [u["id"] for u in por_texto.json()["items"]] == [ajeno.id]


async def test_changes_role_and_status_across_companies(client_admin: AsyncClient, gestor: Sesion) -> None:
    r = await client_admin.patch(f"/admin/usuarios/{gestor.id}", json={"rol": "usuario", "estado": "inactivo"})
    assert r.status_code == 200, r.text
    assert (r.json()["rol"], r.json()["estado"]) == ("usuario", "inactivo")
    assert (await client_admin.patch(f"/admin/usuarios/{gestor.id}", json={})).status_code == 422
    assert (await client_admin.patch(f"/admin/usuarios/{gestor.id}", json={"rol": "dios"})).status_code == 422
    assert (await client_admin.patch("/admin/usuarios/999999999", json={"estado": "activo"})).status_code == 404


async def test_an_admin_cannot_change_their_own_role_or_status(client_admin: AsyncClient, admin: Sesion) -> None:
    for cuerpo in ({"estado": "inactivo"}, {"rol": "gestor"}):
        assert (await client_admin.patch(f"/admin/usuarios/{admin.id}", json=cuerpo)).status_code == 403


async def test_another_admin_can_be_demoted_while_one_stays(
    client_admin: AsyncClient, otra_empresa: dict
) -> None:
    otro_admin = await crear_sesion_en_bd(otra_empresa["id"], "admin")
    r = await client_admin.patch(f"/admin/usuarios/{otro_admin.id}", json={"rol": "gestor"})
    assert r.status_code == 200, r.text


async def test_company_file_and_audit_log(
    client_admin: AsyncClient, admin: Sesion, gestor: Sesion, empresa: dict
) -> None:
    usuario = await crear_sesion_en_bd(empresa["id"], "usuario")
    conv = await crear_convocatoria_en_bd("Ficha admin")
    await crear_alerta_en_bd(gestor.id)
    async with AsyncSessionLocal() as db:
        db.add(Favorito(usuario_id=gestor.id, convocatoria_id=conv.id, created_by=gestor.id, updated_by=gestor.id))
        db.add(
            Seguimiento(
                empresa_id=empresa["id"], convocatoria_id=conv.id, estado="preparando",
                responsable_usuario_id=usuario.id, created_by=gestor.id, updated_by=gestor.id,
            )  # fmt: skip
        )
        await db.commit()

    assert await _lecturas(empresa["id"]) == []
    r = await client_admin.get(f"/admin/empresas/{empresa['id']}")
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["empresa"]["id"] == empresa["id"]
    assert {u["id"] for u in cuerpo["usuarios"]} == {gestor.id, usuario.id}
    por_estado = {s["estado"]: s["total"] for s in cuerpo["seguimiento"]}
    assert por_estado == {"interesa": 0, "preparando": 1, "presentada": 0, "resuelta": 0}
    assert cuerpo["favoritos_total"] == 1 and cuerpo["favoritos"][0]["usuario_id"] == gestor.id
    assert cuerpo["alertas"]["total"] == 1
    assert cuerpo["solicitudes_recientes"] == []
    for secreto in ("password", "token", "calendario"):
        assert secreto not in r.text

    lecturas = await _lecturas(empresa["id"], admin.id)
    assert [(x.accion, x.admin_usuario_id) for x in lecturas] == [("ficha", admin.id)]
    await client_admin.get(f"/admin/empresas/{empresa['id']}")
    assert len(await _lecturas(empresa["id"], admin.id)) == 2

    # Listing the users of a company is a read of that company too.
    await client_admin.get("/admin/usuarios", params={"empresa_id": empresa["id"]})
    assert [x.accion for x in await _lecturas(empresa["id"], admin.id)][-1] == "usuarios"


async def test_unknown_company_is_404_and_not_logged(client_admin: AsyncClient, admin: Sesion) -> None:
    assert (await client_admin.get("/admin/empresas/999999999")).status_code == 404
    async with AsyncSessionLocal() as db:
        n = await db.scalar(
            select(func.count())
            .select_from(AdminLecturaCliente)
            .where(AdminLecturaCliente.admin_usuario_id == admin.id)
        )
    assert n == 0


async def test_alert_counts_use_only_that_companys_users(
    client_admin: AsyncClient, gestor: Sesion, empresa: dict, otra_empresa: dict
) -> None:
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "gestor")
    await crear_alerta_en_bd(ajeno.id)
    r = await client_admin.get(f"/admin/empresas/{empresa['id']}")
    assert r.json()["alertas"] == {"total": 0, "activas": 0}
    assert r.json()["favoritos_total"] == 0
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(Alerta).where(Alerta.usuario_id == gestor.id)) == 0
