"""Pure theme rules (N1): no database."""

import pytest

from app.models.convocatoria import TEMAS
from app.services.clasificacion import CATALOGO_TEMAS, clasificar_tema, gestiona_por_regla


@pytest.mark.parametrize(
    ("titulo", "finalidad", "esperado"),
    [
        ("Programa Kit Digital · Segmento I", None, "dig"),
        ("Ayudas para la digitalización de pymes", None, "dig"),
        ("Subvención para proyectos de Inteligencia Artificial y ciberseguridad", None, "dig"),
        ("Ayudas a la implantación de comercio electrónico", None, "dig"),
        ("Ayudas a proyectos de I+D+i empresariales", None, "id"),
        ("Convocatoria de proyectos de investigación científica", "Investigación, desarrollo e innovación", "id"),
        ("Ayudas a la innovación en pymes", None, "id"),
        ("Ayudas para actuaciones de eficiencia energética en edificios", None, "ene"),
        ("Autoconsumo con energía fotovoltaica", None, "ene"),
        ("Ayudas a la economía circular y reciclaje", None, "ene"),
        ("Plan de internacionalización y exportación de pymes", None, "int"),
        ("Promoción exterior y comercio exterior", None, "int"),
        ("Ayudas a la contratación indefinida de desempleados", None, "emp"),
        ("Programa de fomento del empleo local", None, "emp"),
        ("Subvención a la asociación de vecinos", "Cultura", "otro"),
        ("Subvención genérica", None, "otro"),
        ("Subvención genérica", "Industria y Energía", "ene"),
        ("Subvención genérica", "Fomento del Empleo", "emp"),
        ("Subvención genérica", "Comercio, Turismo y Pymes", "otro"),
        # priority: digital wins over innovation, innovation over energy
        ("Innovación y digitalización del comercio", None, "dig"),
        ("Proyectos de I+D en eficiencia energética", None, "id"),
        # false positives that used to be flagged as managed (bare keywords)
        ("Convenio con la asociación científica de matronas", "Sanidad", "otro"),
        ("Subvención a la investigación policial y seguridad ciudadana", None, "otro"),
        ("Subvención directa a una organización sindical", "Investigación, desarrollo e innovación", "otro"),
        ("Premio a tesis doctorales en materia laboral", None, "otro"),
        ("Beca de formación práctica en tecnología educativa", None, "otro"),
        ("Oferta de empleo público de la Diputación", None, "otro"),
        ("Bolsa de empleo para el proceso selectivo de funcionarios", None, "otro"),
        ("Ayudas a proyectos de investigación industrial y desarrollo experimental", None, "id"),
        ("Ayudas a la innovación empresarial en pymes", None, "id"),
        ("Ayudas a la creación de empleo estable", None, "emp"),
        # guards
        ("Ayudas de movilidad internacional de estudiantes (Vicerrectora de Internacionalización)", None, "otro"),
        ("Contratación pública de servicios de limpieza", None, "otro"),
        ("Cooperación internacional para el desarrollo", "Cooperación internacional para el desarrollo", "otro"),
        # accents / case do not matter
        ("AYUDAS A LA DIGITALIZACIÓN", None, "dig"),
        ("Ayudas a la INNOVACION", None, "id"),
    ],
)
def test_reglas_de_tema(titulo: str, finalidad: str | None, esperado: str) -> None:
    assert clasificar_tema(titulo=titulo, finalidad=finalidad) == esperado


def test_los_objetivos_y_los_sectores_tambien_cuentan() -> None:
    objetivos = ["ART. 25 AYUDAS A PROYECTOS DE INVESTIGACIÓN Y DESARROLLO."]
    assert clasificar_tema(titulo="Ayudas", objetivos=objetivos) == "id"
    informatica = ["Programación, consultoría y otras actividades informáticas"]
    assert clasificar_tema(titulo="Ayudas", sectores=informatica) == "dig"
    assert clasificar_tema(titulo="Ayudas", sectores=["Investigación y desarrollo experimental"]) == "id"
    # broadcasting "programación" and telecom beneficiaries are not a digitalisation grant
    radio = ["Actividades de programación, radiodifusión, agencias de noticias y otras actividades de distribución"]
    assert clasificar_tema(titulo="Ayudas", sectores=radio) == "otro"
    # a broad sector does not classify on its own
    assert clasificar_tema(titulo="Ayudas", sectores=["ACTIVIDADES PROFESIONALES, CIENTÍFICAS Y TÉCNICAS"]) == "otro"


def test_solo_dig_e_id_los_gestiona_subvfy() -> None:
    assert {t for t in TEMAS if gestiona_por_regla(t)} == {"dig", "id"}


def test_el_catalogo_cubre_todos_los_temas_en_orden() -> None:
    assert tuple(t.codigo for t in CATALOGO_TEMAS) == TEMAS
    assert {t.codigo for t in CATALOGO_TEMAS if t.gestiona_subvfy} == {"dig", "id"}
    assert all(t.nombre for t in CATALOGO_TEMAS)


def test_gestiona_exige_beneficiarios_empresa_y_finalidad_no_social() -> None:
    # BDNS 936275 "Programa Aldeas Digitales": digital title, but non-economic beneficiaries and a social purpose.
    no_empresa = ["PERSONAS JURÍDICAS QUE NO DESARROLLAN ACTIVIDAD ECONÓMICA"]
    social = "Servicios Sociales y Promoción Social"
    assert gestiona_por_regla("dig", beneficiarios=no_empresa, finalidad=social) is False
    assert gestiona_por_regla("dig", beneficiarios=no_empresa) is False
    assert gestiona_por_regla("dig", beneficiarios=["PYME Y PERSONAS FÍSICAS QUE DESARROLLAN ACTIVIDAD ECONÓMICA"])
    # One company-like beneficiary among others is enough; no beneficiaries or purpose never blocks.
    assert gestiona_por_regla("id", beneficiarios=[*no_empresa, "GRAN EMPRESA"], finalidad="Sanidad") is False
    assert gestiona_por_regla("id", beneficiarios=[*no_empresa, "GRAN EMPRESA"], finalidad="Comercio, Turismo y Pymes")
    assert gestiona_por_regla("dig") and gestiona_por_regla("id", finalidad=None)
    # A social purpose blocks even with company beneficiaries; other themes stay unmanaged whatever the data.
    assert gestiona_por_regla("dig", beneficiarios=["GRAN EMPRESA"], finalidad="Sanidad") is False
    assert gestiona_por_regla("ene", beneficiarios=["GRAN EMPRESA"]) is False
