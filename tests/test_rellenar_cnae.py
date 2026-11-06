"""`rellenar-cnae` (PR B2): fills `cnae_secciones` of rows synced before the column existed.

The BDNS is a fake object with `detalle()`. `prefijo="TST"` keeps the run on the rows the test
created: the shared database also holds real grants, which a test must never touch.
"""

from typing import Any

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Convocatoria
from app.services.bdns_cliente import BdnsNoDisponible
from app.services.bdns_sync_cliente import CircuitoAbierto
from app.services.cnae_relleno import rellenar_cnae
from tests.conftest import crear_convocatoria_en_bd

PREFIJO_TEST = "TST"


class FalsoCliente:
    def __init__(self, respuestas: dict[str, Any]) -> None:
        self.respuestas, self.pedidos = respuestas, []

    async def detalle(self, codigo: str) -> dict[str, Any] | None:
        self.pedidos.append(codigo)
        r = self.respuestas.get(codigo)
        if isinstance(r, Exception):
            raise r
        return r


def _con_sectores(*codigos: str) -> dict[str, Any]:
    return {"sectores": [{"descripcion": f"s{c}", "codigo": c} for c in codigos]}


async def _secciones(codigo: str) -> list[str]:
    async with AsyncSessionLocal() as db:
        consulta = select(Convocatoria.cnae_secciones).where(Convocatoria.codigo_bdns == codigo)
        return list((await db.scalars(consulta)).one())


async def _sembrar(**extra: Any) -> str:
    return (await crear_convocatoria_en_bd("Relleno", **extra)).codigo_bdns


async def test_fills_only_rows_with_sectors_and_no_sections() -> None:
    pendiente = await _sembrar(sectores=["x"])
    rellena = await _sembrar(sectores=["x"], cnae_secciones=["C"])
    sin_sector = await _sembrar(sectores=[])
    cliente = FalsoCliente({pendiente: _con_sectores("62.01", "84.1"), rellena: _con_sectores("10"), sin_sector: {}})
    async with AsyncSessionLocal() as db:
        r = await rellenar_cnae(db, cliente, limite=50, prefijo=PREFIJO_TEST)
    assert cliente.pedidos == [pendiente]
    assert (r.actualizadas, r.sin_seccion, r.errores) == (1, 0, 0)
    assert await _secciones(pendiente) == ["K", "P"]
    assert await _secciones(rellena) == ["C"]
    assert await _secciones(sin_sector) == []


async def test_counts_unmappable_retired_and_failed_details() -> None:
    sin_mapa = await _sembrar(sectores=["x"])
    retirada = await _sembrar(sectores=["x"])
    caida = await _sembrar(sectores=["x"])
    cliente = FalsoCliente({sin_mapa: _con_sectores("34"), retirada: None, caida: BdnsNoDisponible("503")})
    async with AsyncSessionLocal() as db:
        r = await rellenar_cnae(db, cliente, limite=50, prefijo=PREFIJO_TEST)
    assert sorted(cliente.pedidos) == sorted([sin_mapa, retirada, caida])
    assert (r.actualizadas, r.sin_seccion, r.errores) == (0, 2, 1)  # retired counts as unresolved, not as an error
    assert await _secciones(sin_mapa) == []


async def test_limit_caps_the_batch() -> None:
    codigos = [await _sembrar(sectores=["x"]) for _ in range(3)]
    cliente = FalsoCliente({c: _con_sectores("10") for c in codigos})
    async with AsyncSessionLocal() as db:
        r = await rellenar_cnae(db, cliente, limite=2, prefijo=PREFIJO_TEST)
    assert len(cliente.pedidos) == 2 and r.actualizadas == 2


async def test_an_open_circuit_stops_the_run_but_keeps_what_was_done() -> None:
    a = await _sembrar(sectores=["x"])
    b = await _sembrar(sectores=["x"])  # rows are processed in id order
    cliente = FalsoCliente({a: _con_sectores("10"), b: CircuitoAbierto("abierto")})
    async with AsyncSessionLocal() as db:
        r = await rellenar_cnae(db, cliente, limite=50, prefijo=PREFIJO_TEST)
    assert r.interrumpido is True and r.actualizadas == 1
    assert await _secciones(a) == ["C"]


def test_the_cli_command_reports_the_result_and_fails_when_interrupted(monkeypatch, capsys) -> None:
    from app import cli
    from app.services.cnae_relleno import ResultadoRelleno

    pedidos: list[int] = []

    async def falso(*, limite: int) -> ResultadoRelleno:
        pedidos.append(limite)
        return ResultadoRelleno(actualizadas=3, sin_seccion=1, errores=0, interrumpido=False)

    monkeypatch.setattr(cli, "ejecutar_relleno", falso)
    cli.main(["rellenar-cnae", "--limite", "7"])
    assert pedidos == [7]
    assert "3 actualizadas, 1 sin sección, 0 errores" in capsys.readouterr().out

    async def cortado(*, limite: int) -> ResultadoRelleno:
        return ResultadoRelleno(actualizadas=0, sin_seccion=0, errores=0, interrumpido=True)

    monkeypatch.setattr(cli, "ejecutar_relleno", cortado)
    with pytest.raises(SystemExit) as salida:
        cli.main(["rellenar-cnae"])
    assert salida.value.code == 1
