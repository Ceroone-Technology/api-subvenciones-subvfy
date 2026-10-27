"""Catalog flattening and organ/region resolution (PR2, task 2.5). No DB, no network."""

from typing import Any

import pytest

from app.services.bdns_mapper import RutaOrgano
from app.services.catalogos import ResolvedorCatalogo, aplanar_arbol, normalizar_texto
from tests.fixtures_bdns import cargar_fixture

MINISTERIO = "MINISTERIO DE AGRICULTURA, PESCA Y ALIMENTACIÓN"
DG_ALIMENTACION = "DIRECCIÓN GENERAL DE ALIMENTACIÓN"


def nodo(id_: int, descripcion: str, *hijos: dict[str, Any]) -> dict[str, Any]:
    return {"id": id_, "descripcion": descripcion, "children": list(hijos)}


def regiones() -> list[Any]:
    return aplanar_arbol(cargar_fixture("regiones_real_recortado.json"), tipo="region")


def organos_estado() -> list[Any]:
    return aplanar_arbol(cargar_fixture("organos_estado_real.json"), tipo="organo", nivel_administracion="estado")


# --- normalizar_texto -------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("  Consejería  de   Educación ", "CONSEJERIA DE EDUCACION"),
        ("CONSEJERÍA DE EDUCACIÓN", "CONSEJERIA DE EDUCACION"),
        ("Ñandú", "NANDU"),
    ],
)
def test_normalizar_texto_sube_a_mayusculas_quita_acentos_y_colapsa_espacios(texto: str, esperado: str) -> None:
    assert normalizar_texto(texto) == esperado


# --- aplanar_arbol ----------------------------------------------------------


def test_aplanar_regiones_guarda_nodo_mas_ancestros_codigo_y_padre() -> None:
    por_id = {n.bdns_id: n for n in regiones()}

    asturias = por_id[8]
    assert (asturias.tipo, asturias.padre_bdns_id, asturias.codigo) == ("region", 2, "ES12")
    assert asturias.ruta_ids == (1, 2, 8)
    assert por_id[9].ruta_ids == (1, 2, 8, 9)
    assert por_id[9].codigo == "ES120"
    assert por_id[1].padre_bdns_id is None
    assert por_id[1].codigo == "ES"
    assert por_id[1].descripcion == "ES - ESPAÑA"
    assert por_id[1].nivel_administracion is None


def test_aplanar_organos_calcula_la_ruta_de_texto_normalizada() -> None:
    por_id = {n.bdns_id: n for n in organos_estado()}

    dg = por_id[61]
    assert dg.ruta_ids == (23, 61)
    assert dg.ruta_norm == "MINISTERIO DE AGRICULTURA, PESCA Y ALIMENTACION / DIRECCION GENERAL DE ALIMENTACION"
    assert dg.nivel_administracion == "estado"
    assert dg.codigo is None
    assert por_id[23].ruta_ids == (23,)
    assert len(por_id) == 11


def test_aplanar_ignora_nodos_sin_id_o_descripcion_y_acepta_children_ausente() -> None:
    nodos = aplanar_arbol(
        [
            {"id": 1, "descripcion": "RAIZ", "children": [{"descripcion": "sin id"}, {"id": 2, "descripcion": " "}]},
            {"id": 3},
        ],
        tipo="organo",
        nivel_administracion="local",
    )

    assert [n.bdns_id for n in nodos] == [1]


# --- Regions ----------------------------------------------------------------


def test_resolver_regiones_por_codigo_nuts_devuelve_el_nodo_y_sus_ancestros() -> None:
    resolvedor = ResolvedorCatalogo(regiones())

    assert resolvedor.resolver_regiones(["ES12 - PRINCIPADO DE ASTURIAS"]) == [1, 2, 8]
    assert resolvedor.regiones_sin_resolver == 0


def test_resolver_varias_regiones_une_sin_repetir_y_cuenta_las_que_no_encuentra() -> None:
    resolvedor = ResolvedorCatalogo(regiones())

    ids = resolvedor.resolver_regiones(
        ["ES12 - PRINCIPADO DE ASTURIAS", "ES300 - Madrid", "XX99 - Inventada", "ANDALUCIA"]
    )

    assert ids == [1, 2, 8, 25, 26, 27]
    assert resolvedor.regiones_sin_resolver == 2


# --- Organs -----------------------------------------------------------------


def test_resolver_organo_estado_por_texto_con_acentos_y_mayusculas_distintas() -> None:
    resolvedor = ResolvedorCatalogo(organos_estado())

    assert resolvedor.resolver_organo(RutaOrgano("ESTADO", MINISTERIO, DG_ALIMENTACION)) == [23, 61]
    assert resolvedor.resolver_organo(
        RutaOrgano("Estado", "ministerio de agricultura, pesca y alimentacion", "direccion general de alimentacion")
    ) == [23, 61]
    assert resolvedor.organos_sin_resolver == 0


def test_resolver_organo_solo_con_nivel2_no_cuenta_como_sin_resolver() -> None:
    resolvedor = ResolvedorCatalogo(organos_estado())

    assert resolvedor.resolver_organo(RutaOrgano("ESTADO", MINISTERIO, None)) == [23]
    assert resolvedor.organos_sin_resolver == 0


def test_nivel3_desconocido_conserva_la_ruta_parcial_y_cuenta() -> None:
    resolvedor = ResolvedorCatalogo(organos_estado())

    assert resolvedor.resolver_organo(RutaOrgano("ESTADO", MINISTERIO, "UNIDAD QUE NO EXISTE")) == [23]
    assert resolvedor.organos_sin_resolver == 1


@pytest.mark.parametrize(
    "ruta",
    [
        RutaOrgano("ESTADO", "MINISTERIO INVENTADO", None),
        RutaOrgano("ESTADO", "MINISTERIO INVENTADO", DG_ALIMENTACION),
        RutaOrgano("MARCIANA", MINISTERIO, DG_ALIMENTACION),
        RutaOrgano("ESTADO", None, None),
        RutaOrgano(None, None, None),
    ],
)
def test_organo_sin_coincidencia_da_lista_vacia_y_suma_uno(ruta: RutaOrgano) -> None:
    resolvedor = ResolvedorCatalogo(organos_estado())

    assert resolvedor.resolver_organo(ruta) == []
    assert resolvedor.organos_sin_resolver == 1


def test_el_nivel_de_administracion_separa_arboles_con_el_mismo_texto() -> None:
    nodos = [
        *aplanar_arbol([nodo(1, "ARAGON", nodo(2, "EDUCACION"))], tipo="organo", nivel_administracion="ccaa"),
        *aplanar_arbol([nodo(10, "ARAGON", nodo(11, "EDUCACION"))], tipo="organo", nivel_administracion="local"),
    ]
    resolvedor = ResolvedorCatalogo(nodos)

    assert resolvedor.resolver_organo(RutaOrgano("AUTONOMICA", "Aragon", "Educacion")) == [1, 2]
    assert resolvedor.resolver_organo(RutaOrgano("LOCAL", "Aragon", "Educacion")) == [10, 11]


def test_un_arbol_con_raiz_envolvente_se_resuelve_por_sufijo_de_ruta() -> None:
    nodos = aplanar_arbol(
        [nodo(899, "ADMINISTRACION AUTONOMICA", nodo(900, "PRINCIPADO DE ASTURIAS", nodo(901, "CONSEJERÍA DE SALUD")))],
        tipo="organo",
        nivel_administracion="ccaa",
    )
    resolvedor = ResolvedorCatalogo(nodos)

    assert resolvedor.resolver_organo(RutaOrgano("AUTONOMICA", "PRINCIPADO DE ASTURIAS", "CONSEJERÍA DE SALUD")) == [
        899,
        900,
        901,
    ]
    assert resolvedor.organos_sin_resolver == 0


def test_una_ruta_ambigua_no_se_adivina() -> None:
    nodos = aplanar_arbol(
        [
            nodo(1, "RAMA UNO", nodo(2, "ASTURIAS", nodo(3, "SALUD"))),
            nodo(10, "RAMA DOS", nodo(11, "ASTURIAS", nodo(12, "SALUD"))),
        ],
        tipo="organo",
        nivel_administracion="ccaa",
    )
    resolvedor = ResolvedorCatalogo(nodos)

    assert resolvedor.resolver_organo(RutaOrgano("AUTONOMICA", "Asturias", "Salud")) == []
    assert resolvedor.organos_sin_resolver == 1


def test_aplanar_descarta_ids_fuera_de_int32_y_deja_sin_codigo_los_demasiado_largos() -> None:
    arbol = [
        nodo(2**31, "ES99 - FUERA DE RANGO"),
        nodo(7, "ABCDEFGHIJKLMNOPQRSTU - DEMASIADO LARGO"),
        nodo(8, "ES12 - NORMAL"),
    ]
    por_id = {n.bdns_id: n for n in aplanar_arbol(arbol, tipo="region")}
    assert set(por_id) == {7, 8}
    assert por_id[7].codigo is None and por_id[8].codigo == "ES12"
