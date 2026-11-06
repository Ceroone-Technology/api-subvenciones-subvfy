"""`python -m app.cli seed-prueba`: realistic test data for one account only.

Runs against throwaway users and companies (`TEST-` NIF, test email domain) and reads the real grants of the shared
database without modifying them.
"""

import pytest
from sqlalchemy import func, select

from app import cli
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    Alerta,
    AlertaEjecucion,
    AlertaEjecucionConvocatoria,
    Descarte,
    Empresa,
    EmpresaInteres,
    Favorito,
    PreferenciaAviso,
    Seguimiento,
    SeguimientoTarea,
    Usuario,
)
from app.services.seed_prueba import MARCA, SeedRechazado, sembrar
from tests.conftest import Sesion, crear_alerta_en_bd, crear_convocatoria_en_bd, crear_empresa_en_bd, crear_sesion_en_bd


async def _contar(modelo, *condiciones) -> int:
    async with AsyncSessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(modelo).where(*condiciones)) or 0


async def _sembrar(email: str):
    async with AsyncSessionLocal() as db:
        return await sembrar(db, email)


async def _foto(sesion: Sesion) -> dict:
    """Everything the seed could touch that belongs to somebody else."""
    async with AsyncSessionLocal() as db:
        empresa = await db.get(Empresa, sesion.empresa_id)
        usuario = await db.get(Usuario, sesion.id)
        assert empresa is not None and usuario is not None
        return {
            "favoritos": await _contar(Favorito, Favorito.usuario_id == sesion.id),
            "descartes": await _contar(Descarte, Descarte.usuario_id == sesion.id),
            "alertas": await _contar(Alerta, Alerta.usuario_id == sesion.id),
            "tarjetas": await _contar(Seguimiento, Seguimiento.empresa_id == sesion.empresa_id),
            "preferencias": await _contar(PreferenciaAviso, PreferenciaAviso.usuario_id == sesion.id),
            "intereses": await _contar(EmpresaInteres, EmpresaInteres.empresa_id == sesion.empresa_id),
            "perfil": (empresa.provincia, empresa.empleados, empresa.facturacion_tramo, empresa.proyecto_previsto),
            "telefono": usuario.telefono,
            "cargo": usuario.cargo,
            "actividad": (empresa.actividad_codigo, empresa.actividad_otra),
        }


@pytest.fixture
async def abiertas() -> None:
    """The seed needs open grants; the shared database has them, but a clean one would not."""
    from datetime import date, timedelta

    for dias in (3, 6, 10, 15, 22, 35, 60, 100, 120, 130, 140, 150, 160, 170, 180, 190, 200):
        await crear_convocatoria_en_bd(
            "Abierta de prueba", fecha_inicio_solicitud=date.today() - timedelta(days=5),
            fecha_fin_solicitud=date.today() + timedelta(days=dias), presupuesto_total=100_000 * dias,
        )  # fmt: skip


async def test_refuses_outside_development(
    gestor: Sesion, monkeypatch: pytest.MonkeyPatch, abiertas: None
) -> None:
    antes = await _foto(gestor)
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(SeedRechazado, match="development"):
        await _sembrar(gestor.email)
    assert await _foto(gestor) == antes


async def test_refuses_an_unknown_email(abiertas: None) -> None:
    with pytest.raises(SeedRechazado, match="No existe"):
        await _sembrar("no-existe@test.subvfy.example.com")


def test_the_cli_exits_with_the_reason_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(SystemExit, match="development"):
        cli.main(["seed-prueba", "--email", "cualquiera@test.subvfy.example.com"])


async def test_seeds_the_whole_account(gestor: Sesion, otro_gestor: Sesion, abiertas: None) -> None:
    resumen = await _sembrar(gestor.email)
    assert any(linea.startswith("Favoritos") for linea in resumen.lineas)

    async with AsyncSessionLocal() as db:
        empresa = await db.get(Empresa, gestor.empresa_id)
        usuario = await db.get(Usuario, gestor.id)
        assert empresa is not None and usuario is not None
        assert (empresa.provincia, empresa.empleados, empresa.facturacion_tramo) == ("Madrid", 35, "2m_10m")
        assert empresa.proyecto_previsto and usuario.telefono == "+34 600 123 456"
        assert empresa.sitio_web == "https://www.ejemplo-subvfy.es" and usuario.cargo == "Directora de innovación"
        favoritos = (await db.scalars(select(Favorito).where(Favorito.usuario_id == gestor.id))).all()
        alertas = (await db.scalars(select(Alerta).where(Alerta.usuario_id == gestor.id))).all()
        tarjetas = (await db.scalars(select(Seguimiento).where(Seguimiento.empresa_id == gestor.empresa_id))).all()
    assert len(favoritos) == 8 and all(f.nota and f.nota.endswith(MARCA) for f in favoritos)
    assert await _contar(EmpresaInteres, EmpresaInteres.empresa_id == gestor.empresa_id) == 4
    assert len(alertas) == 3 and all(a.ultima_ejecucion_at is not None for a in alertas)
    assert await _contar(AlertaEjecucion, AlertaEjecucion.alerta_id.in_([a.id for a in alertas])) >= 6
    ids = [a.id for a in alertas]
    assert await _contar(AlertaEjecucionConvocatoria, AlertaEjecucionConvocatoria.alerta_id.in_(ids))
    assert len(tarjetas) == 8 and {t.estado for t in tarjetas} == {"interesa", "preparando", "presentada", "resuelta"}
    assert {t.resultado for t in tarjetas if t.estado == "resuelta"} == {"concedida", "denegada"}
    assert all(t.responsable_usuario_id in (gestor.id, otro_gestor.id) for t in tarjetas)
    assert await _contar(SeguimientoTarea, SeguimientoTarea.seguimiento_id.in_([t.id for t in tarjetas])) > 8
    assert await _contar(Descarte, Descarte.usuario_id == gestor.id) == 2
    assert await _contar(PreferenciaAviso, PreferenciaAviso.usuario_id == gestor.id) == 5


async def test_favorites_close_on_varied_dates(gestor: Sesion, abiertas: None) -> None:
    await _sembrar(gestor.email)
    async with AsyncSessionLocal() as db:
        from app.models import Convocatoria

        fechas = set(
            await db.scalars(
                select(Convocatoria.fecha_fin_solicitud)
                .join(Favorito, Favorito.convocatoria_id == Convocatoria.id)
                .where(Favorito.usuario_id == gestor.id)
            )
        )
    assert len(fechas) >= 6


async def test_it_is_idempotent(gestor: Sesion, abiertas: None) -> None:
    await _sembrar(gestor.email)
    primera = await _foto(gestor)
    resumen = await _sembrar(gestor.email)
    assert await _foto(gestor) == primera
    assert any("no se añaden más" in linea for linea in resumen.lineas)


async def test_it_keeps_the_activity_and_does_not_overwrite_a_phone(gestor: Sesion, abiertas: None) -> None:
    async with AsyncSessionLocal() as db:
        empresa = await db.get(Empresa, gestor.empresa_id)
        usuario = await db.get(Usuario, gestor.id)
        assert empresa is not None and usuario is not None
        empresa.actividad_codigo = "C"
        usuario.telefono = "600 000 111"
        await db.commit()
    await _sembrar(gestor.email)
    async with AsyncSessionLocal() as db:
        empresa = await db.get(Empresa, gestor.empresa_id)
        usuario = await db.get(Usuario, gestor.id)
        assert empresa is not None and usuario is not None
        assert empresa.actividad_codigo == "C" and usuario.telefono == "600 000 111"
        assert empresa.razon_social == "Empresa de Prueba SL"


async def test_it_touches_only_that_account(gestor: Sesion, otro_gestor: Sesion, abiertas: None) -> None:
    """A colleague of the same company and an unrelated company keep exactly what they had."""
    ajena = await crear_empresa_en_bd(razon_social="Ajena SL")
    otro = await crear_sesion_en_bd(ajena["id"], "gestor")
    await crear_alerta_en_bd(otro.id, nombre="Mia")
    await crear_alerta_en_bd(otro_gestor.id, nombre="De mi compañero")
    antes_colega, antes_ajeno = await _foto(otro_gestor), await _foto(otro)
    await _sembrar(gestor.email)
    despues_colega, despues_ajeno = await _foto(otro_gestor), await _foto(otro)
    # The colleague shares the company, so the company-level seed (profile, interests, board) is visible to him;
    # what is his own stays: favorites, alerts, preferences, phone.
    for clave in ("favoritos", "descartes", "alertas", "preferencias", "telefono", "cargo"):
        assert despues_colega[clave] == antes_colega[clave], clave
    assert despues_ajeno == antes_ajeno
    assert await _contar(Alerta, Alerta.usuario_id == otro_gestor.id) == 1


async def test_it_works_without_open_grants_in_the_pool(gestor: Sesion, monkeypatch: pytest.MonkeyPatch) -> None:
    async def vacio(*_a, **_k) -> list:
        return []

    monkeypatch.setattr("app.services.seed_prueba._convocatorias_abiertas", vacio)
    resumen = await _sembrar(gestor.email)
    assert await _contar(Favorito, Favorito.usuario_id == gestor.id) == 0
    assert any("0 añadidos" in linea for linea in resumen.lineas)
