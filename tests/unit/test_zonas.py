"""Zone normalisation for the affinity score (N3): free text and catalogue codes to community ids. No database."""

import pytest

from app.services import zonas
from app.services.zonas import NOMBRES_COMUNIDAD, NUTS2_A_COMUNIDAD, comunidad_de_empresa, comunidad_de_texto


@pytest.mark.parametrize(
    ("texto", "esperada"),
    [
        ("Andalucía", "and"),
        ("  ANDALUCIA ", "and"),
        ("Comunidad de Madrid", "mad"),
        ("Madrid", "mad"),
        ("Sevilla", "and"),
        ("Santa Cruz de Tenerife", "can"),
        ("A Coruña", "gal"),
        ("Castellón", "val"),
        ("Alicante/Alacant", "val"),
        ("Ciudad Real", "clm"),
        ("Castilla-La Mancha", "clm"),
        ("Castilla y León", "cyl"),
        ("León", "cyl"),
        ("Bizkaia", "pva"),
        ("La Rioja", "rio"),
        ("Illes Balears", "bal"),
        ("Cataluña", "cat"),
        ("Girona", "cat"),
    ],
)
def test_texto_libre_se_normaliza_a_comunidad(texto: str, esperada: str) -> None:
    assert comunidad_de_texto(texto) == esperada


@pytest.mark.parametrize("texto", [None, "", "   ", "Narnia", "Villes", "Lisboa"])
def test_texto_desconocido_no_inventa_zona(texto: str | None) -> None:
    assert comunidad_de_texto(texto) is None


def test_ceuta_y_melilla_no_tienen_zona_en_el_mapa() -> None:
    assert comunidad_de_texto("Ceuta") is None
    assert zonas.comunidad_de_nuts("ES63") is None and zonas.comunidad_de_nuts("ES64") is None


def test_la_comunidad_manda_sobre_la_provincia_y_la_provincia_completa() -> None:
    assert comunidad_de_empresa("Galicia", "Sevilla") == "gal"
    assert comunidad_de_empresa(None, "Sevilla") == "and"
    assert comunidad_de_empresa("Narnia", "Valencia") == "val"
    assert comunidad_de_empresa(None, None) is None


def test_cada_codigo_nuts_apunta_a_una_comunidad_del_mapa() -> None:
    assert set(NUTS2_A_COMUNIDAD.values()) == set(NOMBRES_COMUNIDAD)
    assert zonas.comunidad_de_nuts("es52") == "val"
    assert zonas.comunidad_de_nuts("ES709") is None  # a province node is not a community
    assert zonas.comunidad_de_nuts("ES2") is None  # nor is a NUTS-1 macro-zone
    assert zonas.comunidad_de_nuts(None) is None


def test_comunidades_de_una_convocatoria_sin_repetidos_y_en_orden() -> None:
    por_region = {10: "cnt", 21: "pva", 99: "pva"}
    assert zonas.comunidades_de_regiones([1, 21, 99, 10], por_region) == ("pva", "cnt")
    assert zonas.comunidades_de_regiones([1], por_region) == ()


def test_nombres_de_zona() -> None:
    assert zonas.nombre_de_zona("ES") == "toda España"
    assert zonas.nombre_de_zona("val") == "C. Valenciana"
    assert zonas.nombre_de_zona(None) == "otra comunidad"
