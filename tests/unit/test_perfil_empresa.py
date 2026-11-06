"""Pure completeness of the company profile (N2): no database."""

from app.models import Empresa
from app.services.perfil_empresa import ITEMS_PERFIL, completitud


def empresa(**campos: object) -> Empresa:
    return Empresa(razon_social="X", nif="X", **campos)  # type: ignore[arg-type]


COMPLETA = {
    "actividad_codigo": "C",
    "tamano": "pequena",
    "provincia": "Valencia",
    "facturacion_tramo": "2m_10m",
    "proyecto_previsto": "Renovar la línea de mecanizado",
}


def test_perfil_vacio_es_cero_y_lo_pide_todo() -> None:
    pct, pendientes = completitud(empresa(), [])
    assert pct == 0
    assert [p.codigo for p in pendientes] == [i.codigo for i in ITEMS_PERFIL]


def test_perfil_completo_es_cien_y_no_pide_nada() -> None:
    pct, pendientes = completitud(empresa(**COMPLETA), ["erp", "energia", "formacion"])
    assert (pct, pendientes) == (100, [])


def test_cada_item_suma_veinte() -> None:
    assert completitud(empresa(actividad_codigo="C"), [])[0] == 20
    assert completitud(empresa(facturacion_tramo="mas_50m"), [])[0] == 20
    assert completitud(empresa(proyecto_previsto="algo"), [])[0] == 20
    assert completitud(empresa(), ["erp", "energia", "formacion"])[0] == 20
    assert completitud(empresa(tamano="micro", provincia="Soria"), [])[0] == 20


def test_tamano_y_ubicacion_van_juntos() -> None:
    assert completitud(empresa(tamano="micro"), [])[0] == 0
    assert completitud(empresa(provincia="Soria"), [])[0] == 0
    assert completitud(empresa(tamano="micro", provincia="  "), [])[0] == 0


def test_hacen_falta_tres_intereses() -> None:
    assert completitud(empresa(), ["erp", "energia"])[0] == 0
    assert completitud(empresa(), ["erp", "erp", "energia"])[0] == 0  # repeated ones do not add up


def test_un_proyecto_en_blanco_no_cuenta() -> None:
    assert completitud(empresa(proyecto_previsto="   "), [])[0] == 0


def test_un_item_que_falta_deja_el_ochenta() -> None:
    sin_proyecto = {**COMPLETA, "proyecto_previsto": None}
    pct, pendientes = completitud(empresa(**sin_proyecto), ["erp", "energia", "formacion"])
    assert pct == 80 and [p.codigo for p in pendientes] == ["proyecto"]
