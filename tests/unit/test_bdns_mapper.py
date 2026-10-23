"""Detail -> `convocatoria` row mapper (PR2, task 2.4). Pure, no DB, no network."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.models import Convocatoria
from app.services.bdns_mapper import (
    DetalleInvalido,
    RutaOrgano,
    es_plazo_permanente,
    etiquetas_region,
    mapear_detalle,
    ruta_organo,
)
from tests.fixtures_bdns import cargar_fixture


def detalle_real() -> dict[str, Any]:
    return cargar_fixture("detalle_real.json")


def detalle_con_documentos() -> dict[str, Any]:
    return cargar_fixture("detalle_con_documentos_real.json")


def test_mapea_el_detalle_grabado_a_columnas() -> None:
    fila = mapear_detalle(detalle_real())

    assert fila["codigo_bdns"] == "924160"
    assert fila["bdns_id"] == 1125721
    assert fila["titulo"] == "SUBVENCION NOMINATIVA COLEGIO ABOGADOS DE OVIEDO"
    assert fila["nivel_administracion"] == "ccaa"
    assert fila["administracion"] == "PRINCIPADO DE ASTURIAS"
    assert fila["organo_convocante"] == "CONSEJERÍA DE PRESIDENCIA, RETO DEMOGRÁFICO, IGUALDAD Y TURISMO"
    assert fila["fecha_registro"] == date(2026, 8, 7)
    assert fila["fecha_inicio_solicitud"] == date(2026, 1, 1)
    assert fila["fecha_fin_solicitud"] == date(2026, 7, 29)
    assert fila["abierto_bdns"] is False
    assert fila["financiada_mrr"] is False
    assert fila["presupuesto_total"] == Decimal("7500.00")
    assert fila["tipo_convocatoria"] == "Concesión directa - instrumental"
    assert fila["finalidad"] == "Servicios Sociales y Promoción Social"
    assert fila["url_bases_reguladoras"] == "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-2888"
    assert fila["sede_electronica"] is None
    assert fila["beneficiarios"] == ["PERSONAS JURÍDICAS QUE NO DESARROLLAN ACTIVIDAD ECONÓMICA"]
    assert fila["sectores"] == ["Administración pública y de la política económica, social y medioambiental"]
    assert fila["plazo_permanente"] is False
    assert fila["organo_ids"] == []
    assert fila["region_ids"] == []


def test_mapea_documentos_fondos_e_instrumentos_al_detalle_compacto() -> None:
    fila = mapear_detalle(detalle_con_documentos())

    assert fila["financiada_mrr"] is True
    assert fila["presupuesto_total"] == Decimal("25838818.00")
    assert fila["nivel_administracion"] == "ccaa"
    compacto = fila["detalle"]
    assert compacto["documentos"] == [
        {
            "id": 1507071,
            "nombre": "TextoConvocatoria.pdf",
            "descripcion": "Texto en castellano de la convocatoria",
            "tamano_bytes": 412269,
            "fecha_modificacion": "2026-08-07",
            "fecha_publicacion": "2026-08-07",
        }
    ]
    assert compacto["fondos"] == ["MRR - MECANISMO DE RECUPERACIÓN Y RESILIENCIA"]
    assert compacto["instrumentos"] == ["SUBVENCIÓN Y ENTREGA DINERARIA SIN CONTRAPRESTACIÓN"]
    assert compacto["regiones"] == ["ES61 - ANDALUCIA"]
    assert compacto["objetivos"] == []
    assert compacto["bases_reguladoras"].startswith("Conv. Público.")


def test_los_ids_resueltos_se_copian_a_la_fila() -> None:
    fila = mapear_detalle(detalle_real(), organo_ids=[1, 5], region_ids=(1, 2, 8))

    assert fila["organo_ids"] == [1, 5]
    assert fila["region_ids"] == [1, 2, 8]


def test_las_columnas_de_texto_se_recortan_al_limite_del_modelo() -> None:
    detalle = detalle_real()
    detalle["descripcion"] = "x" * 900
    detalle["urlBasesReguladoras"] = "https://e.example/" + "y" * 2000

    fila = mapear_detalle(detalle)

    assert len(fila["titulo"]) == Convocatoria.__table__.c.titulo.type.length == 500
    assert len(fila["url_bases_reguladoras"]) == 1000


def test_un_detalle_sin_codigo_o_sin_titulo_es_invalido() -> None:
    sin_codigo = detalle_real()
    sin_codigo["codigoBDNS"] = None
    sin_titulo = detalle_real()
    sin_titulo["descripcion"] = "  "

    with pytest.raises(DetalleInvalido, match="codigoBDNS"):
        mapear_detalle(sin_codigo)
    with pytest.raises(DetalleInvalido, match="descripcion"):
        mapear_detalle(sin_titulo)


def test_campos_ausentes_o_mal_tipados_quedan_en_none_sin_romper() -> None:
    detalle = {
        "codigoBDNS": "1",
        "descripcion": "Solo lo obligatorio",
        "presupuestoTotal": "mucho",
        "fechaFinSolicitud": "no es fecha",
        "regiones": "no es lista",
        "documentos": [None, {"descripcion": "sin id"}],
    }

    fila = mapear_detalle(detalle)

    assert fila["presupuesto_total"] is None
    assert fila["fecha_fin_solicitud"] is None
    assert fila["nivel_administracion"] is None
    assert fila["beneficiarios"] == []
    assert fila["detalle"]["regiones"] == []
    assert fila["detalle"]["documentos"] == []
    assert fila["plazo_permanente"] is False


def test_las_fechas_con_hora_se_recortan_a_la_fecha() -> None:
    detalle = detalle_real()
    detalle["fechaFinSolicitud"] = "2026-09-30T23:59:59"

    assert mapear_detalle(detalle)["fecha_fin_solicitud"] == date(2026, 9, 30)


# --- Hash -----------------------------------------------------------------


def test_el_hash_es_estable_y_de_64_hex() -> None:
    primero = mapear_detalle(detalle_real())["contenido_hash"]
    segundo = mapear_detalle(deepcopy(detalle_real()))["contenido_hash"]

    assert primero == segundo
    assert len(primero) == 64
    int(primero, 16)


def test_el_hash_cambia_con_el_contenido_y_con_los_ids_resueltos() -> None:
    base = mapear_detalle(detalle_real())["contenido_hash"]
    prorrogado = detalle_real()
    prorrogado["fechaFinSolicitud"] = "2026-11-15"
    con_documento = detalle_con_documentos()

    assert mapear_detalle(prorrogado)["contenido_hash"] != base
    assert mapear_detalle(con_documento)["contenido_hash"] != base
    assert mapear_detalle(detalle_real(), organo_ids=[3])["contenido_hash"] != base
    assert mapear_detalle(detalle_real(), region_ids=[8])["contenido_hash"] != base


def test_el_hash_ignora_campos_que_no_guardamos() -> None:
    ruido = detalle_real()
    ruido["anuncios"] = [{"id": 1}]
    ruido["advertencia"] = "cualquier cosa"

    assert mapear_detalle(ruido)["contenido_hash"] == mapear_detalle(detalle_real())["contenido_hash"]


# --- Organ and regions as inputs of the resolution -------------------------


def test_ruta_organo_lee_los_tres_niveles() -> None:
    assert ruta_organo(detalle_real()) == RutaOrgano(
        "AUTONOMICA", "PRINCIPADO DE ASTURIAS", "CONSEJERÍA DE PRESIDENCIA, RETO DEMOGRÁFICO, IGUALDAD Y TURISMO"
    )
    assert ruta_organo({"codigoBDNS": "1"}) == RutaOrgano(None, None, None)


def test_etiquetas_region_devuelve_las_descripciones_sin_vacias() -> None:
    assert etiquetas_region(detalle_con_documentos()) == ["ES61 - ANDALUCIA"]
    assert etiquetas_region({"regiones": [{"descripcion": " "}, {"descripcion": "ES12 - X"}, {}]}) == ["ES12 - X"]
    assert etiquetas_region({}) == []


# --- plazo_permanente ------------------------------------------------------


@pytest.mark.parametrize(
    ("texto_inicio", "texto_fin", "fecha_fin", "esperado"),
    [
        (None, "Abierto de forma permanente", None, True),
        ("Convocatoria abierta de manera PERMANENTE", None, None, True),
        (None, "Plazo indefinido", None, True),
        (None, "Siempre abierta", None, True),
        # The recorded "Sin plazo de solicitud" is a direct grant, not an open call.
        ("Sin plazo de solicitur", "Sin plazo de solicitud", None, False),
        (None, None, None, False),
        # With an end date the call is not permanent whatever the text says.
        (None, "Abierto de forma permanente", date(2026, 12, 31), False),
    ],
)
def test_plazo_permanente_solo_si_el_texto_lo_dice_y_no_hay_fecha_fin(
    texto_inicio: str | None, texto_fin: str | None, fecha_fin: date | None, esperado: bool
) -> None:
    assert es_plazo_permanente(texto_inicio, texto_fin, fecha_fin) is esperado


def test_el_detalle_sin_fechas_pero_permanente_se_marca() -> None:
    detalle = detalle_real()
    detalle["fechaInicioSolicitud"] = None
    detalle["fechaFinSolicitud"] = None
    detalle["textFin"] = "Convocatoria abierta de forma permanente"

    assert mapear_detalle(detalle)["plazo_permanente"] is True
    assert mapear_detalle(detalle_con_documentos())["plazo_permanente"] is False
