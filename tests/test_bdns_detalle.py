"""Detalle de una convocatoria en la BDNS (Hito 5, H5.2; lo reutiliza H5.3).

**No se llama a la BDNS real**: se inyecta un `httpx.MockTransport`. Las fichas
de `tests/fixtures/bdns/` sí son respuestas reales de la BDNS (datos públicos),
guardadas el 2026-10-04, para que el parseo se pruebe contra la forma de verdad
y no contra la que suponemos:

- `detalle_900000.json`: plazo con fechas, sin reglamento.
- `detalle_933305.json`: plazo en texto ("Día siguiente a la publicación en
  DOE"), reglamento de minimis y 21 sectores.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.bdns_cliente import (
    BdnsNoDisponible,
    ClienteBdns,
    ConvocatoriaNoEncontrada,
    DocumentoBdns,
)

FICHAS = Path(__file__).parent / "fixtures" / "bdns"


def _ficha(codigo: str) -> dict[str, Any]:
    return json.loads((FICHAS / f"detalle_{codigo}.json").read_text(encoding="utf-8"))


def _cliente(manejador) -> ClienteBdns:
    return ClienteBdns(
        httpx.AsyncClient(transport=httpx.MockTransport(manejador), base_url="https://bdns.example/api")
    )


def _responde(respuesta: httpx.Response):
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return respuesta

    return manejador


# --- Fichas reales --------------------------------------------------------


async def test_una_ficha_real_se_lee_completa() -> None:
    async with _cliente(_responde(httpx.Response(200, json=_ficha("900000")))) as cliente:
        detalle = await cliente.obtener_detalle("900000")

    assert (detalle.id_bdns, detalle.codigo_bdns) == (1101561, "900000")
    assert detalle.titulo is not None and detalle.titulo.startswith("Convenio de colaboración")
    assert detalle.fecha_registro == date(2026, 4, 20)
    assert (detalle.nivel1, detalle.nivel2) == ("LOCAL", "CASTELLÓN DE LA PLANA/CASTELLÓ DE LA PLANA")
    assert detalle.nivel3 == "AYUNTAMIENTO DE CASTELLÓN DE LA PLANA/CASTELLÓ DE LA PLANA"
    assert detalle.financiada_mrr is False
    assert detalle.tipo_convocatoria == "Concesión directa - instrumental"
    assert detalle.finalidad == "Cultura"
    # La BDNS lo manda con un espacio al final.
    assert detalle.instrumentos == ("SUBVENCIÓN Y ENTREGA DINERARIA SIN CONTRAPRESTACIÓN",)
    assert detalle.tipos_beneficiario == ("PERSONAS JURÍDICAS QUE NO DESARROLLAN ACTIVIDAD ECONÓMICA",)
    assert detalle.sectores == ("ACTIVIDADES ARTÍSTICAS, DEPORTIVAS Y DE ENTRETENIMIENTO",)
    assert detalle.regiones == ("ES522 - Castellón / Castelló",)
    assert detalle.presupuesto_total == Decimal("25000")
    assert detalle.abierta is False
    assert (detalle.fecha_inicio_solicitud, detalle.fecha_fin_solicitud) == (date(2025, 2, 13), date(2025, 12, 31))
    assert (detalle.texto_inicio_solicitud, detalle.texto_fin_solicitud) == (None, None)
    assert detalle.url_bases_reguladoras == "https://www.castello.es/es/pressupost"
    assert detalle.reglamento is None
    assert (detalle.fondos, detalle.objetivos) == ((), ())
    assert detalle.documentos == (
        DocumentoBdns(
            descripcion="Documento de la convocatoria en español", nombre_fichero="Certificado Acuerdo VAM.pdf"
        ),
    )


async def test_una_ficha_real_con_el_plazo_en_texto_y_reglamento() -> None:
    async with _cliente(_responde(httpx.Response(200, json=_ficha("933305")))) as cliente:
        detalle = await cliente.obtener_detalle("933305")

    assert (detalle.fecha_inicio_solicitud, detalle.fecha_fin_solicitud) == (None, None)
    assert detalle.texto_inicio_solicitud == "Día siguiente a la publicación en DOE"
    assert detalle.texto_fin_solicitud == "12 meses desde fecha inicio"
    assert detalle.reglamento == "REG (UE) 2023/2831 de minimis, General"
    assert detalle.tipos_beneficiario == ("PYME Y PERSONAS FÍSICAS QUE DESARROLLAN ACTIVIDAD ECONÓMICA",)
    assert len(detalle.sectores) == 21
    assert detalle.presupuesto_total == Decimal("700000")


async def test_la_peticion_pide_el_codigo_sin_espacios() -> None:
    peticiones: list[httpx.Request] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return httpx.Response(200, json=_ficha("900000"))

    async with _cliente(manejador) as cliente:
        await cliente.obtener_detalle("  900000 ")

    assert peticiones[0].url.path == "/api/convocatorias"
    assert list(peticiones[0].url.params.multi_items()) == [("numConv", "900000")]


# --- Código que no existe o no vale ---------------------------------------


@pytest.mark.parametrize("respuesta", [httpx.Response(204), httpx.Response(200, content=b"  ")])
async def test_un_codigo_que_no_existe_es_convocatoria_no_encontrada(respuesta: httpx.Response) -> None:
    """La BDNS contesta 204 sin cuerpo, no 404 (comprobado el 2026-10-04)."""
    async with _cliente(_responde(respuesta)) as cliente:
        with pytest.raises(ConvocatoriaNoEncontrada) as error:
            await cliente.obtener_detalle("999999999")
    assert error.value.codigo == "999999999"


@pytest.mark.parametrize("codigo", ["", "   ", "1" * 31])
async def test_un_codigo_vacio_o_demasiado_largo_no_llega_a_la_bdns(codigo: str) -> None:
    peticiones: list[httpx.Request] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return httpx.Response(200, json=_ficha("900000"))

    async with _cliente(manejador) as cliente:
        with pytest.raises(ValueError):
            await cliente.obtener_detalle(codigo)
    assert peticiones == []


# --- Fallos de la BDNS ----------------------------------------------------


async def test_la_pagina_html_del_cortafuegos_es_bdns_no_disponible() -> None:
    """Con algunos parámetros, la BDNS contesta 200 con una página de "Acceso denegado"."""
    html = httpx.Response(200, text="<html><h1>Acceso Denegado</h1></html>", headers={"content-type": "text/html"})
    async with _cliente(_responde(html)) as cliente:
        with pytest.raises(BdnsNoDisponible):
            await cliente.obtener_detalle("900000")


async def test_un_error_http_en_el_detalle_es_bdns_no_disponible() -> None:
    async with _cliente(_responde(httpx.Response(503))) as cliente:
        with pytest.raises(BdnsNoDisponible) as error:
            await cliente.obtener_detalle("900000")
    assert error.value.status == 503


async def test_un_timeout_en_el_detalle_es_bdns_no_disponible() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=peticion)

    async with _cliente(manejador) as cliente:
        with pytest.raises(BdnsNoDisponible):
            await cliente.obtener_detalle("900000")


@pytest.mark.parametrize("cuerpo", [[], {"descripcion": "sin id"}, {"id": "no-numerico"}])
async def test_un_detalle_sin_identificador_es_bdns_no_disponible(cuerpo: Any) -> None:
    async with _cliente(_responde(httpx.Response(200, json=cuerpo))) as cliente:
        with pytest.raises(BdnsNoDisponible):
            await cliente.obtener_detalle("900000")


# --- Parseo defensivo -----------------------------------------------------


async def test_un_detalle_minimo_no_revienta() -> None:
    async with _cliente(_responde(httpx.Response(200, json={"id": 1}))) as cliente:
        detalle = await cliente.obtener_detalle("1")

    assert detalle.id_bdns == 1
    assert (detalle.codigo_bdns, detalle.titulo, detalle.nivel1, detalle.presupuesto_total) == (None, None, None, None)
    assert (detalle.sectores, detalle.regiones, detalle.documentos) == ((), (), ())
    assert detalle.abierta is None
    assert detalle.financiada_mrr is False


async def test_campos_con_forma_inesperada_no_revientan() -> None:
    cuerpo = {
        "id": 1,
        "organo": "no es un objeto",
        "sectores": "no es una lista",
        "regiones": [{"descripcion": "  "}, {"otro": "x"}, "ES30 - MADRID"],
        "presupuestoTotal": "mucho",
        "abierto": "sí",
        "reglamento": "texto suelto",
        "fechaFinSolicitud": "31/12/2026",
        "documentos": ["no es un objeto", {"nombreFic": "bases.pdf"}],
    }
    async with _cliente(_responde(httpx.Response(200, json=cuerpo))) as cliente:
        detalle = await cliente.obtener_detalle("1")

    assert detalle.nivel1 is None
    assert detalle.sectores == ()
    assert detalle.regiones == ("ES30 - MADRID",)
    assert detalle.presupuesto_total is None
    assert detalle.abierta is None
    assert detalle.reglamento is None
    assert detalle.fecha_fin_solicitud is None
    assert detalle.documentos == (DocumentoBdns(descripcion=None, nombre_fichero="bases.pdf"),)
