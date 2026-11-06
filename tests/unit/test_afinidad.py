"""Affinity score (N3): every block, its reasons and doubts. Pure, no database."""

from dataclasses import replace

from app.services.afinidad import PerfilAfinidad, SenalesConvocatoria, calcular

PERFIL = PerfilAfinidad(
    actividad_codigo="C", tamano="pequena", comunidad="val", intereses=frozenset({"erp", "ciberseguridad"})
)
PERFECTA = SenalesConvocatoria(
    tema="dig",
    cnae_secciones=["C", "J"],
    tiene_sectores=True,
    comunidades=["val"],
    beneficiarios=["PYME Y PERSONAS FÍSICAS QUE DESARROLLAN ACTIVIDAD ECONÓMICA"],
    dias_restantes=40,
)


def con(**cambios: object) -> SenalesConvocatoria:
    return replace(PERFECTA, **cambios)  # type: ignore[arg-type]


def perfil(**cambios: object) -> PerfilAfinidad:
    return replace(PERFIL, **cambios)  # type: ignore[arg-type]


def test_todo_encaja_son_cien_con_cuatro_motivos_y_sin_dudas() -> None:
    r = calcular(PERFIL, PERFECTA)
    assert r.afinidad == 100
    assert r.motivos == [
        "Encaja con la actividad de tu empresa",
        "Sede en C. Valenciana",
        "Dirigida a pymes y autónomos",
        "Interés: ciberseguridad, erp y gestión",
    ]
    assert r.dudas == []
    assert r.zona == "val"


def test_actividad_sin_sector_declarado_vale_el_70_por_ciento() -> None:
    r = calcular(PERFIL, con(cnae_secciones=[], tiene_sectores=False))
    assert r.afinidad == 100 - 40 + 28
    assert "Abierta a todos los sectores" in r.motivos


def test_actividad_de_otros_sectores_no_puntua_y_lo_dice() -> None:
    r = calcular(PERFIL, con(cnae_secciones=["A"]))
    assert r.afinidad == 60
    assert r.dudas == ["Va dirigida a otros sectores de actividad"]


def test_sectores_declarados_pero_sin_resolver_no_puntuan() -> None:
    r = calcular(PERFIL, con(cnae_secciones=[], tiene_sectores=True))
    assert r.afinidad == 60
    assert r.dudas == ["Declara sectores que aún no hemos podido comprobar contra tu actividad"]


def test_actividad_pendiente_u_otra_pide_completar_el_perfil() -> None:
    for codigo in (None, "OTRA"):
        r = calcular(perfil(actividad_codigo=codigo), PERFECTA)
        assert r.afinidad == 60
        assert r.dudas == ["Indica la actividad principal (CNAE) de tu empresa para comprobar el sector"]


def test_zona_estatal_vale_14_y_otra_comunidad_cero() -> None:
    estatal = calcular(PERFIL, con(comunidades=[], estatal=True))
    assert estatal.afinidad == 94 and "Ámbito estatal" in estatal.motivos and estatal.zona == "ES"
    otra = calcular(PERFIL, con(comunidades=["gal"]))
    assert otra.afinidad == 80
    assert otra.dudas == ["Es de otra comunidad (Galicia)"] and otra.zona == "gal"


def test_estatal_que_lista_comunidades_ajenas_sigue_siendo_estatal() -> None:
    r = calcular(PERFIL, con(comunidades=["gal"], estatal=True))
    assert r.afinidad == 94 and r.zona == "gal"


def test_sin_provincia_no_se_puede_comprobar_la_zona() -> None:
    sin_zona = perfil(comunidad=None)
    r = calcular(sin_zona, con(comunidades=["gal"]))
    assert r.afinidad == 80
    assert r.dudas == ["Indica tu provincia para comprobar si la ayuda es de tu zona"]
    assert calcular(sin_zona, con(comunidades=[], estatal=True)).afinidad == 94


def test_zona_no_indicada_se_avisa() -> None:
    r = calcular(PERFIL, con(comunidades=[], estatal=False))
    assert r.afinidad == 80 and r.zona is None
    assert r.dudas == ["No indica la zona a la que se dirige"]


def test_beneficiarios_contra_tamano() -> None:
    gran = ["GRAN EMPRESA"]
    pyme_en_gran = calcular(PERFIL, con(beneficiarios=gran))
    assert pyme_en_gran.afinidad == 85 and pyme_en_gran.dudas == ["Está pensada para gran empresa"]
    grande = perfil(tamano="grande")
    assert "Dirigida a gran empresa" in calcular(grande, con(beneficiarios=gran)).motivos
    grande_en_pyme = calcular(grande, PERFECTA)
    assert grande_en_pyme.afinidad == 85 and grande_en_pyme.dudas == ["Está pensada para pymes y autónomos"]
    assert calcular(perfil(tamano="autonomo"), PERFECTA).afinidad == 100


def test_beneficiarios_sin_actividad_economica_no_encajan() -> None:
    sin = [
        "PERSONAS JURÍDICAS QUE NO DESARROLLAN ACTIVIDAD ECONÓMICA",
        "PERSONAS FÍSICAS QUE NO DESARROLLAN ACTIVIDAD ECONÓMICA",
    ]
    r = calcular(PERFIL, con(beneficiarios=sin))
    assert r.afinidad == 85
    assert r.dudas == ["Va dirigida a entidades y personas sin actividad económica"]


def test_sin_beneficiarios_declarados_vale_la_mitad_sin_inventar_nada() -> None:
    r = calcular(PERFIL, con(beneficiarios=[]))
    assert r.afinidad == 92
    assert "Dirigida a pymes y autónomos" not in r.motivos and r.dudas == []


def test_sin_tamano_se_pide_si_la_convocatoria_lo_declara() -> None:
    sin_tamano = perfil(tamano=None)
    r = calcular(sin_tamano, PERFECTA)
    assert r.afinidad == 85 and r.dudas == ["Indica el tamaño de tu empresa para comprobar a quién se dirige"]
    assert calcular(sin_tamano, con(beneficiarios=[])).dudas == []


def test_intereses() -> None:
    sin_interes_del_tema = calcular(PERFIL, con(tema="ene"))
    assert sin_interes_del_tema.afinidad == 85
    assert sin_interes_del_tema.dudas == ["No coincide con los intereses de tu perfil"]
    assert calcular(PERFIL, con(tema="otro")).dudas == []  # nothing to compare an "other" grant with
    r = calcular(perfil(intereses=frozenset()), PERFECTA)
    assert r.afinidad == 85
    assert r.dudas == ["Aún no has marcado intereses: añádelos para afinar la afinidad"]


def test_plazo() -> None:
    assert calcular(PERFIL, con(dias_restantes=21)).afinidad == 100
    assert calcular(PERFIL, con(dias_restantes=20)).afinidad == 96
    assert calcular(PERFIL, con(dias_restantes=10)).afinidad == 96
    urgente = calcular(PERFIL, con(dias_restantes=9))
    assert urgente.afinidad == 92
    assert urgente.dudas == ["Cierra en 9 días: poco margen para preparar la solicitud"]
    assert calcular(PERFIL, con(dias_restantes=1)).dudas == ["Cierra en 1 día: poco margen para preparar la solicitud"]
    assert calcular(PERFIL, con(dias_restantes=None)).afinidad == 95
    cerrada = calcular(PERFIL, con(dias_restantes=-3))
    assert cerrada.afinidad == 90 and cerrada.dudas == ["El plazo de solicitud ya ha terminado"]


def test_perfil_vacio_no_inventa_motivos_y_queda_acotado() -> None:
    r = calcular(PerfilAfinidad(), SenalesConvocatoria())
    assert r.motivos == []
    assert r.afinidad == 7 + 5 and len(r.dudas) == 3
