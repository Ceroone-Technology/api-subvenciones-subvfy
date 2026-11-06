"""Constructor de la consulta a la BDNS (Hito 4, tarea 2).

`construir_consulta` es pura, así que estos tests no necesitan base de datos ni
red: la `Alerta` se construye en memoria. Los nombres y valores de los
parámetros están verificados contra la API real de la BDNS.
"""

from datetime import date

import httpx
import pytest

from app.models import Alerta
from app.services.bdns_consulta import (
    NIVEL_A_TIPO_ADMINISTRACION,
    ConsultaBdns,
    CriteriosAlertaInvalidos,
    construir_consulta,
)

DESDE = date(2026, 9, 1)


def _alerta(**criterios) -> Alerta:
    return Alerta(id=1, usuario_id=1, nombre="Alerta de prueba", **criterios)


def _query(consulta: ConsultaBdns) -> str:
    """La query tal como la montaría el cliente HTTP, que es lo que importa."""
    return str(httpx.URL("https://bdns.example/busqueda", params=consulta.parametros()).query, "utf-8")


# --- Cada criterio a su parámetro ---------------------------------------


def test_el_texto_va_en_descripcion() -> None:
    consulta = construir_consulta(_alerta(texto_busqueda="digitalización"), [], [], desde=DESDE)
    assert consulta.descripcion == "digitalización"
    assert ("descripcion", "digitalización") in consulta.parametros()


@pytest.mark.parametrize(("nivel", "esperado"), sorted(NIVEL_A_TIPO_ADMINISTRACION.items()))
def test_el_nivel_se_mapea_a_tipo_administracion(nivel: str, esperado: str) -> None:
    """Nuestros valores no son los de la BDNS: estado→C, ccaa→A, local→L, otros→O."""
    consulta = construir_consulta(_alerta(nivel_administracion=nivel), [], [], desde=DESDE)
    assert consulta.tipo_administracion == esperado
    assert ("tipoAdministracion", esperado) in consulta.parametros()


def test_organos_y_regiones_se_repiten_en_una_sola_consulta() -> None:
    """La BDNS acumula los ids repetidos, así que no hay producto cartesiano."""
    consulta = construir_consulta(_alerta(), [1500, 42], [6, 13], desde=DESDE)
    assert consulta.parametros().count(("organos", "1500")) == 1
    assert [valor for clave, valor in consulta.parametros() if clave == "regiones"] == ["6", "13"]
    assert _query(consulta).count("organos=") == 2


def test_las_fechas_van_en_formato_bdns() -> None:
    """dd/mm/yyyy en los filtros, aunque la respuesta venga en yyyy-mm-dd."""
    # La del usuario es posterior al desde del ciclo, así que es la que manda.
    consulta = construir_consulta(
        _alerta(fecha_desde=date(2026, 9, 4), fecha_hasta=date(2026, 12, 31)), [], [], desde=DESDE
    )
    parametros = dict(consulta.parametros())
    assert parametros["fechaDesde"] == "04/09/2026"
    assert parametros["fechaHasta"] == "31/12/2026"


def test_sin_criterios_opcionales_no_aparecen_parametros_vacios() -> None:
    consulta = construir_consulta(_alerta(texto_busqueda="pymes"), [], [], desde=DESDE)
    claves = {clave for clave, _ in consulta.parametros()}
    assert claves == {"descripcion", "fechaDesde"}


def test_varios_criterios_a_la_vez() -> None:
    consulta = construir_consulta(
        _alerta(texto_busqueda="I+D", nivel_administracion="ccaa", solo_mrr=True),
        [1500],
        [9],
        desde=DESDE,
    )
    assert dict(consulta.parametros()) == {
        "descripcion": "I+D",
        "tipoAdministracion": "A",
        "organos": "1500",
        "regiones": "9",
        "fechaDesde": "01/09/2026",
    }
    # solo_mrr no viaja a la BDNS: no tiene parámetro, lo filtra el cliente.
    assert consulta.solo_mrr is True
    assert "mrr" not in _query(consulta)


# --- La fecha desde -----------------------------------------------------


def test_la_fecha_desde_del_ciclo_se_aplica_siempre() -> None:
    consulta = construir_consulta(_alerta(texto_busqueda="x"), [], [], desde=date(2026, 9, 20))
    assert consulta.fecha_desde == date(2026, 9, 20)


def test_entre_las_dos_fechas_desde_gana_la_mas_restrictiva() -> None:
    """Lo incremental no reabre un rango que el usuario había cerrado."""
    posterior = construir_consulta(_alerta(fecha_desde=date(2026, 9, 10)), [], [], desde=DESDE)
    anterior = construir_consulta(_alerta(fecha_desde=date(2026, 1, 1)), [], [], desde=DESDE)
    assert posterior.fecha_desde == date(2026, 9, 10)  # la del usuario, más restrictiva
    assert anterior.fecha_desde == DESDE  # la del ciclo, más restrictiva


# --- Criterios que no dan consulta --------------------------------------


def test_una_alerta_sin_criterios_no_se_puede_consultar() -> None:
    """Traería la BDNS entera: es error de dominio, no una consulta vacía."""
    with pytest.raises(CriteriosAlertaInvalidos) as error:
        construir_consulta(_alerta(), [], [], desde=DESDE)
    assert "ningún criterio" in str(error.value)


def test_solo_mrr_no_cuenta_como_criterio() -> None:
    """No acota la consulta: solo recorta lo que ya se ha traído."""
    with pytest.raises(CriteriosAlertaInvalidos):
        construir_consulta(_alerta(solo_mrr=True), [], [], desde=DESDE)


def test_un_rango_ya_pasado_no_se_puede_consultar() -> None:
    with pytest.raises(CriteriosAlertaInvalidos) as error:
        construir_consulta(_alerta(fecha_hasta=date(2026, 8, 1)), [], [], desde=DESDE)
    assert "no hay nada que consultar" in str(error.value)


# --- Texto libre del usuario -------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        "ayudas & subvenciones",
        "clave=valor",
        "I+D+i",
        "digitalización de pymes en Andalucía",
        "100% subvencionado",
        "?page=0#top",
    ],
)
def test_el_texto_libre_no_rompe_la_consulta(texto: str) -> None:
    """Se pasa como valor y lo codifica el cliente HTTP: nada de pegarlo a la
    URL a mano. Al leer la query de vuelta tiene que salir intacto."""
    consulta = construir_consulta(_alerta(texto_busqueda=texto), [], [], desde=DESDE)
    url = httpx.URL("https://bdns.example/busqueda", params=consulta.parametros())
    assert url.params["descripcion"] == texto
    assert "&descripcion=" not in _query(consulta).removeprefix("descripcion=")
