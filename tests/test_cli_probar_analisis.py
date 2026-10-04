"""Comando `probar-analisis` (Hito 5, H5.2).

**Ni la BDNS ni Anthropic reales**: se inyectan los dos clientes con un
`httpx.MockTransport` (la ficha es una respuesta real guardada, y Anthropic es
el simulador compartido). La base de datos sí es real: el comando lee de ella
el perfil de la empresa, y se comprueba que no escribe nada.
"""

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.cli import AVISO_COSTE, main, probar_analisis
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import AnalisisIA, Convocatoria, Empresa
from app.services.bdns_cliente import ClienteBdns
from app.services.ia_prompts import SISTEMA
from tests.conftest import FICHAS_BDNS, crear_empresa_en_bd
from tests.simulador_anthropic import CLAVE_FALSA, SimuladorAnthropic

FICHA_933305 = json.loads((FICHAS_BDNS / "detalle_933305.json").read_text(encoding="utf-8"))


class BdnsSimulada:
    """Contesta siempre lo mismo y cuenta las peticiones."""

    def __init__(self, respuesta: httpx.Response | None = None) -> None:
        self.respuesta = respuesta or httpx.Response(200, json=FICHA_933305)
        self.peticiones = 0

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones += 1
        return self.respuesta

    def cliente(self) -> ClienteBdns:
        return ClienteBdns(httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="https://bdns.example/api"))


async def _contar_filas() -> tuple[int, int]:
    async with AsyncSessionLocal() as db:
        analisis = (await db.execute(select(func.count()).select_from(AnalisisIA))).scalar_one()
        convocatorias = (await db.execute(select(func.count()).select_from(Convocatoria))).scalar_one()
    return analisis, convocatorias


# --- Aviso de coste y paradas previas ----------------------------------------


async def test_sin_clave_avisa_del_coste_y_no_llama_a_nadie(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    bdns = BdnsSimulada()

    codigo = await probar_analisis(codigo="933305", cliente_bdns=bdns.cliente())

    salida = capsys.readouterr().out
    assert codigo == 2
    assert salida.startswith(AVISO_COSTE)
    assert "ANTHROPIC_API_KEY" in salida
    assert bdns.peticiones == 0


async def test_una_empresa_que_no_existe_para_antes_de_la_bdns(capsys: pytest.CaptureFixture[str]) -> None:
    bdns, anthropic = BdnsSimulada(), SimuladorAnthropic()

    codigo = await probar_analisis(
        codigo="933305", empresa_id=0, cliente_bdns=bdns.cliente(), cliente_ia=anthropic.cliente()
    )

    assert codigo == 2
    assert "No existe ninguna empresa con id 0" in capsys.readouterr().out
    assert bdns.peticiones == 0
    assert anthropic.cuerpos == {}


@pytest.mark.parametrize(
    ("codigo_bdns", "respuesta", "mensaje"),
    [
        ("999999999", httpx.Response(204), "no tiene ninguna convocatoria"),
        ("abc", httpx.Response(200, json=FICHA_933305), "entre 1 y 30 dígitos"),
        ("933305", httpx.Response(503), "La BDNS respondió 503"),
    ],
)
async def test_sin_convocatoria_no_se_llama_a_la_ia(
    codigo_bdns: str, respuesta: httpx.Response, mensaje: str, capsys: pytest.CaptureFixture[str]
) -> None:
    anthropic = SimuladorAnthropic()

    codigo = await probar_analisis(
        codigo=codigo_bdns, cliente_bdns=BdnsSimulada(respuesta).cliente(), cliente_ia=anthropic.cliente()
    )

    salida = capsys.readouterr().out
    assert codigo == 2
    assert "No se pudo obtener la convocatoria" in salida
    assert mensaje in salida
    assert anthropic.cuerpos == {}


# --- Análisis ---------------------------------------------------------------------


async def test_sin_empresa_imprime_resumen_y_requisitos_con_tokens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "anthropic_precio_entrada_millon", None)
    anthropic = SimuladorAnthropic()

    codigo = await probar_analisis(
        codigo="933305", cliente_bdns=BdnsSimulada().cliente(), cliente_ia=anthropic.cliente()
    )

    salida = capsys.readouterr().out
    assert codigo == 0
    assert salida.startswith(AVISO_COSTE)
    assert "Convocatoria 933305: Ayudas 2026/2027, del Programa VI" in salida
    assert "== resumen (resumen-v1) ==" in salida
    assert "== requisitos_clave (requisitos_clave-v1) ==" in salida
    assert "idoneidad" not in salida
    assert "Tokens: 1000 de entrada, 200 de salida | Coste estimado: sin calcular" in salida
    assert '"aviso_bases": "Este análisis se basa solo en los datos publicados en la BDNS' in salida
    assert "Total: 2000 tokens de entrada, 400 de salida, coste estimado sin calcular." in salida
    assert "ANTHROPIC_PRECIO_ENTRADA_MILLON" in salida
    assert set(anthropic.cuerpos) == {"registrar_resumen", "registrar_requisitos_clave"}


async def test_con_precios_imprime_el_coste_de_cada_analisis_y_el_total(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "anthropic_precio_entrada_millon", Decimal("3"))
    monkeypatch.setattr(settings, "anthropic_precio_salida_millon", Decimal("15"))

    await probar_analisis(
        codigo="933305", cliente_bdns=BdnsSimulada().cliente(), cliente_ia=SimuladorAnthropic().cliente()
    )

    salida = capsys.readouterr().out
    # 1000 × 3 + 200 × 15 = 6000 por millón → 0,006 USD por análisis.
    assert "Coste estimado: 0.0060 USD" in salida
    assert "coste estimado 0.0120 USD." in salida


async def test_con_empresa_anade_la_idoneidad_y_no_escribe_en_la_base_de_datos(
    capsys: pytest.CaptureFixture[str],
) -> None:
    datos = await crear_empresa_en_bd(razon_social="TEST-CONFIDENCIAL Bar Pepe SL")
    async with AsyncSessionLocal() as db:
        empresa = await db.get(Empresa, datos["id"])
        assert empresa is not None
        empresa.sector, empresa.ccaa, empresa.descripcion = "Hostelería", "Extremadura", "Bar en Mérida."
        await db.commit()
    antes = await _contar_filas()
    anthropic = SimuladorAnthropic()

    codigo = await probar_analisis(
        codigo="933305", empresa_id=datos["id"], cliente_bdns=BdnsSimulada().cliente(), cliente_ia=anthropic.cliente()
    )

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "== idoneidad (idoneidad-v1) ==" in salida
    assert '"encaje": "alto"' in salida
    assert await _contar_filas() == antes
    # La razón social no viaja a la IA (ni sale en la pantalla).
    assert "TEST-CONFIDENCIAL" not in json.dumps(anthropic.cuerpos, ensure_ascii=False)
    assert "TEST-CONFIDENCIAL" not in salida


async def test_un_analisis_que_falla_se_muestra_y_el_codigo_de_salida_es_1(capsys: pytest.CaptureFixture[str]) -> None:
    anthropic = SimuladorAnthropic(fallos={"registrar_requisitos_clave": 529})

    codigo = await probar_analisis(
        codigo="933305", cliente_bdns=BdnsSimulada().cliente(), cliente_ia=anthropic.cliente()
    )

    salida = capsys.readouterr().out
    assert codigo == 1
    assert "== requisitos_clave ==\nERROR (IANoDisponible): Anthropic no está disponible ahora (529)." in salida
    assert "== resumen (resumen-v1) ==" in salida
    assert "Total: 1000 tokens de entrada, 200 de salida" in salida


async def test_no_imprime_ni_el_prompt_ni_la_clave(capsys: pytest.CaptureFixture[str]) -> None:
    await probar_analisis(
        codigo="933305", cliente_bdns=BdnsSimulada().cliente(), cliente_ia=SimuladorAnthropic().cliente()
    )

    salida = capsys.readouterr().out
    assert "<convocatoria>" not in salida
    assert SISTEMA[:60] not in salida
    assert CLAVE_FALSA not in salida


# --- Línea de comandos --------------------------------------------------------


def test_la_ayuda_avisa_de_que_cuesta_dinero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "CUESTA DINERO" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        main(["probar-analisis", "--help"])
    assert "llamadas de pago" in capsys.readouterr().out


def test_el_subcomando_devuelve_el_codigo_de_salida(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    with pytest.raises(SystemExit) as salida:
        main(["probar-analisis", "--codigo", "933305"])

    assert salida.value.code == 2
    assert capsys.readouterr().out.startswith(AVISO_COSTE)
