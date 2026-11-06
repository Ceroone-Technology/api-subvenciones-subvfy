"""`python -m app.cli crear-cuentas-prueba`: role-testing accounts (development only).

The module's emails are swapped for throwaway ones on the test domain so the suite never touches the real accounts
and the teardown cleans these up.
"""

import logging
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app import cli
from app.config import settings
from app.core.security import verificar_password
from app.database import AsyncSessionLocal
from app.models import Alerta, Empresa, EmpresaInteres, Favorito, Rol, Seguimiento, Usuario
from app.services import cuentas_prueba
from app.services.cuentas_prueba import CuentasRechazadas, crear_cuentas
from app.services.perfil_empresa import completitud
from tests.conftest import Sesion, crear_convocatoria_en_bd, email_de_prueba, nif_de_prueba


@pytest.fixture(autouse=True)
def datos_de_prueba(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    gestor, usuario = email_de_prueba("gestor-cp"), email_de_prueba("usuario-cp")
    monkeypatch.setattr(cuentas_prueba, "EMAIL_GESTOR", gestor)
    monkeypatch.setattr(cuentas_prueba, "EMAIL_USUARIO", usuario)
    monkeypatch.setattr(cuentas_prueba, "NIF", nif_de_prueba())  # the teardown deletes the company
    return gestor, usuario


emails_de_prueba = datos_de_prueba


async def _crear(email: str):
    async with AsyncSessionLocal() as db:
        return await crear_cuentas(db, email)


async def _usuarios(*emails: str) -> dict[str, tuple[Usuario, str]]:
    async with AsyncSessionLocal() as db:
        filas = (
            await db.execute(
                select(Usuario, Rol.codigo).join(Rol, Rol.id == Usuario.rol_id).where(Usuario.email.in_(emails))
            )
        ).all()
        return {u.email: (u, rol) for u, rol in filas}


async def _convocatorias_abiertas(n: int = 12) -> None:
    dia = date.today()
    for i in range(n):
        await crear_convocatoria_en_bd(
            f"Abierta {i}",
            fecha_inicio_solicitud=dia - timedelta(days=5),
            fecha_fin_solicitud=dia + timedelta(days=3 + i * 4),
        )


async def _asignadas(usuario_id: int) -> int:
    async with AsyncSessionLocal() as db:
        consulta = select(Seguimiento.id).where(Seguimiento.responsable_usuario_id == usuario_id)
        return len(list(await db.scalars(consulta)))


async def test_refuses_outside_development(
    gestor: Sesion, monkeypatch: pytest.MonkeyPatch, emails_de_prueba: tuple[str, str]
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(CuentasRechazadas, match="development"):
        await _crear(gestor.email)
    assert await _usuarios(*emails_de_prueba) == {}


def test_the_cli_exits_with_the_reason_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(SystemExit, match="development"):
        cli.main(["crear-cuentas-prueba", "--email", "cualquiera@test.subvfy.example.com"])


async def test_creates_a_separate_client_company_with_both_accounts(
    gestor: Sesion, emails_de_prueba: tuple[str, str]
) -> None:
    await _convocatorias_abiertas()
    resultado = await _crear(gestor.email)
    cuentas = await _usuarios(*emails_de_prueba)
    e_gestor, e_usuario = emails_de_prueba
    assert cuentas[e_gestor][1] == "gestor" and cuentas[e_usuario][1] == "usuario"
    assert {c.email for c in resultado.cuentas} == set(emails_de_prueba)
    assert resultado.empresa == "Cliente de Prueba S.L."
    empresa_id = cuentas[e_gestor][0].empresa_id
    assert empresa_id != gestor.empresa_id  # never the reference user's company
    assert all(u.empresa_id == empresa_id and u.estado == "activo" for u, _ in cuentas.values())
    async with AsyncSessionLocal() as db:
        empresa = await db.get(Empresa, empresa_id)
        assert empresa is not None
        pct, pendientes = completitud(empresa, await _intereses(db, empresa_id))
        assert (pct, pendientes) == (100, [])


async def _intereses(db, empresa_id: int) -> list[str]:
    return list(await db.scalars(select(EmpresaInteres.interes_codigo).where(EmpresaInteres.empresa_id == empresa_id)))


async def test_seeds_favourites_two_alerts_and_the_board(gestor: Sesion, emails_de_prueba: tuple[str, str]) -> None:
    await _convocatorias_abiertas()
    await _crear(gestor.email)
    g, _ = (await _usuarios(emails_de_prueba[0]))[emails_de_prueba[0]]
    async with AsyncSessionLocal() as db:
        favoritos = await db.scalar(select(func.count()).select_from(Favorito).where(Favorito.usuario_id == g.id))
        alertas = await db.scalar(select(func.count()).select_from(Alerta).where(Alerta.usuario_id == g.id))
        tarjetas = await db.scalar(
            select(func.count()).select_from(Seguimiento).where(Seguimiento.empresa_id == g.empresa_id)
        )
    assert favoritos and favoritos > 0
    assert alertas == 2
    assert tarjetas and tarjetas >= 2


async def test_password_hash_verifies_and_is_random(gestor: Sesion, emails_de_prueba: tuple[str, str]) -> None:
    resultado = await _crear(gestor.email)
    cuentas = await _usuarios(*emails_de_prueba)
    for c in resultado.cuentas:
        assert len(c.password) >= 16
        assert verificar_password(c.password, cuentas[c.email][0].password_hash)
    assert resultado.cuentas[0].password != resultado.cuentas[1].password


async def test_assigns_two_cards_to_the_usuario_and_is_idempotent(
    gestor: Sesion, emails_de_prueba: tuple[str, str]
) -> None:
    await _convocatorias_abiertas()
    await _crear(gestor.email)
    uid = (await _usuarios(emails_de_prueba[1]))[emails_de_prueba[1]][0].id
    assert await _asignadas(uid) == 2

    async with AsyncSessionLocal() as db:
        antes = await db.scalar(select(func.count()).select_from(Seguimiento))
    await _crear(gestor.email)
    assert await _asignadas(uid) == 2
    assert len(await _usuarios(*emails_de_prueba)) == 2
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(Seguimiento)) == antes


async def test_rerun_resets_the_password_and_reactivates(gestor: Sesion, emails_de_prueba: tuple[str, str]) -> None:
    primera = await _crear(gestor.email)
    async with AsyncSessionLocal() as db:
        u = await db.scalar(select(Usuario).where(Usuario.email == emails_de_prueba[0]))
        assert u is not None
        u.estado = "bloqueado"
        await db.commit()
    segunda = await _crear(gestor.email)
    cuentas = await _usuarios(*emails_de_prueba)
    u, _ = cuentas[emails_de_prueba[0]]
    assert u.estado == "activo"
    assert not segunda.cuentas[0].creada
    nueva = segunda.cuentas[0].password
    assert nueva != primera.cuentas[0].password
    assert verificar_password(nueva, u.password_hash)
    assert not verificar_password(primera.cuentas[0].password, u.password_hash)


async def test_passwords_never_reach_the_logs(gestor: Sesion, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    resultado = await _crear(gestor.email)
    for c in resultado.cuentas:
        assert c.password not in caplog.text


async def test_refuses_an_unknown_reference_user() -> None:
    with pytest.raises(CuentasRechazadas, match="No existe"):
        await _crear("no-existe@test.subvfy.example.com")
