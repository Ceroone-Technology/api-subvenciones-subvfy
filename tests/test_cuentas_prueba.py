"""`python -m app.cli crear-cuentas-prueba`: role-testing accounts (development only).

The module's emails are swapped for throwaway ones on the test domain so the suite never touches the real accounts
and the teardown cleans these up.
"""

import logging

import pytest
from sqlalchemy import select

from app import cli
from app.config import settings
from app.core.security import verificar_password
from app.database import AsyncSessionLocal
from app.models import Rol, Seguimiento, Usuario
from app.services import cuentas_prueba
from app.services.cuentas_prueba import CuentasRechazadas, crear_cuentas
from tests.conftest import Sesion, crear_convocatoria_en_bd, email_de_prueba


@pytest.fixture(autouse=True)
def emails_de_prueba(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    gestor, usuario = email_de_prueba("gestor-cp"), email_de_prueba("usuario-cp")
    monkeypatch.setattr(cuentas_prueba, "EMAIL_GESTOR", gestor)
    monkeypatch.setattr(cuentas_prueba, "EMAIL_USUARIO", usuario)
    return gestor, usuario


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


async def _tarjetas(gestor: Sesion, n: int = 4) -> None:
    async with AsyncSessionLocal() as db:
        for i in range(n):
            c = await crear_convocatoria_en_bd(f"Tarjeta {i}")
            db.add(
                Seguimiento(
                    empresa_id=gestor.empresa_id, convocatoria_id=c.id,
                    estado="preparando" if i % 2 == 0 else "interesa",
                    responsable_usuario_id=gestor.id, created_by=gestor.id, updated_by=gestor.id,
                )  # fmt: skip
            )
        await db.commit()


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


async def test_creates_both_accounts_with_the_right_roles(gestor: Sesion, emails_de_prueba: tuple[str, str]) -> None:
    await _tarjetas(gestor)
    resultado = await _crear(gestor.email)
    cuentas = await _usuarios(*emails_de_prueba)
    e_gestor, e_usuario = emails_de_prueba
    assert cuentas[e_gestor][1] == "gestor" and cuentas[e_usuario][1] == "usuario"
    assert all(u.empresa_id == gestor.empresa_id and u.estado == "activo" for u, _ in cuentas.values())
    assert {c.email for c in resultado.cuentas} == set(emails_de_prueba)


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
    await _tarjetas(gestor)
    await _crear(gestor.email)
    uid = (await _usuarios(emails_de_prueba[1]))[emails_de_prueba[1]][0].id
    assert await _asignadas(uid) == 2
    async with AsyncSessionLocal() as db:
        asignadas = list(await db.scalars(select(Seguimiento).where(Seguimiento.responsable_usuario_id == uid)))
    assert all(t.estado == "preparando" for t in asignadas)

    await _crear(gestor.email)
    assert await _asignadas(uid) == 2
    assert len(await _usuarios(*emails_de_prueba)) == 2


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
