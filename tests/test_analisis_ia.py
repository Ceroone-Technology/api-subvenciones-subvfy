"""Análisis con IA de una convocatoria (Hito 5, H5.2).

**No se llama a Anthropic real**: se usa el simulador de
`tests/simulador_anthropic.py` (el SDK real sobre un `httpx.MockTransport`).
Sin base de datos.

Lo que se fija aquí es lo que pone el código y no la IA: el aviso de las
bases, la regla del perfil incompleto, los éxitos parciales y que el perfil
solo viaja en la llamada de idoneidad.
"""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.config import settings
from app.services.analisis_ia import (
    AVISO_BASES,
    AnalisisGenerado,
    PerfilInsuficiente,
    analizar_convocatoria,
    analizar_idoneidad,
    analizar_requisitos,
    analizar_resumen,
)
from app.services.ia_cliente import IANoDisponible, IARespuestaInvalida
from app.services.ia_entrada import DATO_ACTIVIDAD, DATO_CCAA, PERSONA_JURIDICA, PerfilEmpresa
from app.services.ia_prompts import MAX_TOKENS_ANALISIS, VERSIONES_PROMPT
from tests.conftest import ficha_bdns
from tests.simulador_anthropic import RESPUESTAS, SimuladorAnthropic

HOY = date(2026, 10, 4)
FICHA = ficha_bdns("900000")
PERFIL = PerfilEmpresa(
    sector="Cultura",
    tamano="pequena",
    ccaa="Comunitat Valenciana",
    descripcion="TEST-CONFIDENCIAL organización de festivales de música.",
    palabras_clave=("música", "festivales"),
    tipo_persona=PERSONA_JURIDICA,
)
SIN_ACTIVIDAD = replace(PERFIL, descripcion=None, palabras_clave=())
VACIO = PerfilEmpresa(None, None, None, None, (), PERSONA_JURIDICA)

# --- Resumen y requisitos ---------------------------------------------------


async def test_resumen_con_version_modelo_tokens_y_sin_aviso(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_precio_entrada_millon", Decimal("3"))
    monkeypatch.setattr(settings, "anthropic_precio_salida_millon", Decimal("15"))

    async with SimuladorAnthropic().cliente() as cliente:
        analisis = await analizar_resumen(cliente, FICHA)

    assert analisis.tipo == "resumen"
    assert analisis.version_prompt == VERSIONES_PROMPT["resumen"]
    assert analisis.modelo == "claude-sonnet-5-20260101"
    assert (analisis.tokens_entrada, analisis.tokens_salida) == (1000, 200)
    # 1000 × 3 + 200 × 15 = 6000 por millón → 0,006 USD.
    assert analisis.coste_estimado == Decimal("0.0060")
    assert analisis.resultado.puntos_clave == ["25.000 €", "Concesión directa"]
    assert "aviso_bases" not in analisis.resultado.model_dump()


async def test_sin_precios_el_coste_queda_vacio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_precio_entrada_millon", None)

    async with SimuladorAnthropic().cliente() as cliente:
        analisis = await analizar_resumen(cliente, FICHA)

    assert analisis.coste_estimado is None


async def test_los_requisitos_llevan_siempre_el_aviso_del_codigo() -> None:
    async with SimuladorAnthropic().cliente() as cliente:
        analisis = await analizar_requisitos(cliente, FICHA)

    resultado = analisis.resultado
    assert resultado.aviso_bases == AVISO_BASES
    assert "no incluye las bases reguladoras" in resultado.aviso_bases
    assert resultado.url_bases_reguladoras == "https://www.castello.es/es/pressupost"
    assert resultado.beneficiarios == ["Personas jurídicas sin actividad económica."]
    assert resultado.sectores == []


async def test_el_aviso_sale_aunque_la_ficha_no_traiga_enlace_a_las_bases() -> None:
    async with SimuladorAnthropic().cliente() as cliente:
        analisis = await analizar_requisitos(cliente, replace(FICHA, url_bases_reguladoras=None))

    assert analisis.resultado.aviso_bases == AVISO_BASES
    assert analisis.resultado.url_bases_reguladoras is None


async def test_una_respuesta_que_no_cumple_el_formato_es_invalida() -> None:
    simulador = SimuladorAnthropic(datos={"registrar_requisitos_clave": {"beneficiarios": ["sin indicador"]}})

    async with simulador.cliente() as cliente:
        with pytest.raises(IARespuestaInvalida):
            await analizar_requisitos(cliente, FICHA)


# --- Idoneidad y regla del perfil -----------------------------------------


async def test_idoneidad_con_perfil_completo_respeta_el_encaje_de_la_ia() -> None:
    async with SimuladorAnthropic().cliente() as cliente:
        analisis = await analizar_idoneidad(cliente, FICHA, PERFIL, hoy=HOY)

    resultado = analisis.resultado
    assert analisis.version_prompt == VERSIONES_PROMPT["idoneidad"]
    assert resultado.encaje == "alto"
    assert resultado.encaje_limitado_por_perfil is False
    assert resultado.datos_perfil_que_faltan == []
    assert resultado.aviso_bases == AVISO_BASES


async def test_sin_actividad_un_alto_se_rebaja_a_medio_y_queda_marcado() -> None:
    async with SimuladorAnthropic().cliente() as cliente:
        analisis = await analizar_idoneidad(cliente, FICHA, SIN_ACTIVIDAD, hoy=HOY)

    assert analisis.resultado.encaje == "medio"
    assert analisis.resultado.encaje_limitado_por_perfil is True
    assert analisis.resultado.datos_perfil_que_faltan == [DATO_ACTIVIDAD]


@pytest.mark.parametrize("encaje", ["medio", "bajo", "no_encaja"])
async def test_sin_actividad_un_encaje_que_no_es_alto_no_se_toca(encaje: str) -> None:
    simulador = SimuladorAnthropic(
        datos={"registrar_idoneidad": {**RESPUESTAS["registrar_idoneidad"], "encaje": encaje}}
    )

    async with simulador.cliente() as cliente:
        analisis = await analizar_idoneidad(cliente, FICHA, SIN_ACTIVIDAD, hoy=HOY)

    assert analisis.resultado.encaje == encaje
    assert analisis.resultado.encaje_limitado_por_perfil is False


async def test_si_falta_otro_dato_decide_la_ia_y_se_le_dice() -> None:
    """El "alto" no se toca: la IA sabe qué falta y si importa en esta convocatoria."""
    simulador = SimuladorAnthropic()

    async with simulador.cliente() as cliente:
        analisis = await analizar_idoneidad(cliente, FICHA, replace(PERFIL, ccaa=None), hoy=HOY)

    assert analisis.resultado.encaje == "alto"
    assert analisis.resultado.encaje_limitado_por_perfil is False
    assert analisis.resultado.datos_perfil_que_faltan == [DATO_CCAA]
    mensaje = simulador.cuerpos["registrar_idoneidad"]["messages"][0]["content"]
    assert "Datos del perfil que faltan: comunidad autónoma." in mensaje


async def test_con_el_perfil_vacio_no_se_llama_a_la_ia() -> None:
    simulador = SimuladorAnthropic()

    async with simulador.cliente() as cliente:
        with pytest.raises(PerfilInsuficiente):
            await analizar_idoneidad(cliente, FICHA, VACIO, hoy=HOY)

    assert simulador.cuerpos == {}


# --- Los tres a la vez --------------------------------------------------------


async def test_con_perfil_se_lanzan_los_tres_y_solo_la_idoneidad_lo_recibe() -> None:
    simulador = SimuladorAnthropic()

    async with simulador.cliente() as cliente:
        resultados = await analizar_convocatoria(cliente, FICHA, PERFIL, hoy=HOY)

    assert set(resultados) == {"resumen", "requisitos_clave", "idoneidad"}
    assert all(isinstance(analisis, AnalisisGenerado) for analisis in resultados.values())
    # Confidencialidad entre clientes: resumen y requisitos se comparten.
    for herramienta in ("registrar_resumen", "registrar_requisitos_clave"):
        assert "TEST-CONFIDENCIAL" not in json.dumps(simulador.cuerpos[herramienta], ensure_ascii=False)
    assert "TEST-CONFIDENCIAL" in json.dumps(simulador.cuerpos["registrar_idoneidad"], ensure_ascii=False)


async def test_sin_perfil_solo_resumen_y_requisitos() -> None:
    simulador = SimuladorAnthropic()

    async with simulador.cliente() as cliente:
        resultados = await analizar_convocatoria(cliente, FICHA, hoy=HOY)

    assert set(resultados) == {"resumen", "requisitos_clave"}
    assert set(simulador.cuerpos) == {"registrar_resumen", "registrar_requisitos_clave"}


async def test_un_fallo_no_se_lleva_por_delante_los_demas() -> None:
    """Los análisis que salieron bien ya se han pagado: no se pierden."""
    simulador = SimuladorAnthropic(fallos={"registrar_idoneidad": 529})

    async with simulador.cliente() as cliente:
        resultados = await analizar_convocatoria(cliente, FICHA, PERFIL, hoy=HOY)

    assert isinstance(resultados["idoneidad"], IANoDisponible)
    assert isinstance(resultados["resumen"], AnalisisGenerado)
    assert isinstance(resultados["requisitos_clave"], AnalisisGenerado)


async def test_el_perfil_vacio_es_un_resultado_mas_y_no_cuesta_nada() -> None:
    simulador = SimuladorAnthropic()

    async with simulador.cliente() as cliente:
        resultados = await analizar_convocatoria(cliente, FICHA, VACIO, hoy=HOY)

    assert isinstance(resultados["idoneidad"], PerfilInsuficiente)
    assert isinstance(resultados["resumen"], AnalisisGenerado)
    assert "registrar_idoneidad" not in simulador.cuerpos


async def test_un_error_que_no_es_de_la_ia_se_propaga(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fallo nuestro no se disfraza de resultado parcial."""

    async def roto(*args: Any, **kwargs: Any) -> None:
        raise KeyError("fallo de programación")

    monkeypatch.setattr("app.services.analisis_ia.analizar_requisitos", roto)

    async with SimuladorAnthropic().cliente() as cliente:
        with pytest.raises(KeyError):
            await analizar_convocatoria(cliente, FICHA, hoy=HOY)


async def test_cada_llamada_pide_el_tope_de_tokens_del_analisis() -> None:
    simulador = SimuladorAnthropic()

    async with simulador.cliente() as cliente:
        await analizar_convocatoria(cliente, FICHA, PERFIL, hoy=HOY)

    assert {cuerpo["max_tokens"] for cuerpo in simulador.cuerpos.values()} == {MAX_TOKENS_ANALISIS}


async def test_una_lista_con_textos_vacios_no_tumba_la_respuesta_pagada() -> None:
    simulador = SimuladorAnthropic(
        datos={"registrar_requisitos_clave": {**RESPUESTAS["registrar_requisitos_clave"], "cuantia": [""]}}
    )

    async with simulador.cliente() as cliente:
        analisis = await analizar_requisitos(cliente, FICHA)

    assert analisis.resultado.cuantia == []
