"""Alertas (Hito 4, Funcionalidad 1): mutaciones y filtros de órgano/región.

Como en favoritos, los tests de endpoint van de punta a punta contra la base
de datos real y comprueban en ella lo que se ha persistido: la respuesta
podría estar bien aunque las filas hijas no lo estuvieran.
"""

from datetime import datetime

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import (
    Alerta,
    AlertaEjecucion,
    AlertaEjecucionConvocatoria,
    AlertaOrgano,
    AlertaRegion,
    Convocatoria,
)
from app.schemas.alerta import MAX_FILTROS, AlertaCreate, AlertaUpdate
from app.services.filtros import normalizar_ids_bdns
from tests.conftest import Sesion, codigo_bdns_de_prueba, crear_sesion_en_bd

ID_INEXISTENTE = 2**62


def _alerta(**extra) -> dict:
    return {"nombre": "Digitalización en Andalucía", **extra}


async def _filtros_en_bd(alerta_id: int) -> tuple[list[int], list[int]]:
    async with AsyncSessionLocal() as db:
        organos = await db.scalars(
            select(AlertaOrgano.organo_bdns_id)
            .where(AlertaOrgano.alerta_id == alerta_id)
            .order_by(AlertaOrgano.id)
        )
        regiones = await db.scalars(
            select(AlertaRegion.region_bdns_id)
            .where(AlertaRegion.alerta_id == alerta_id)
            .order_by(AlertaRegion.id)
        )
        return list(organos), list(regiones)


async def _crear(client: AsyncClient, **extra) -> dict:
    respuesta = await client.post("/alertas", json=_alerta(**extra))
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


# --- Normalización (unitarios, sin HTTP) ---------------------------------


def test_normalizar_quita_duplicados_conservando_el_orden() -> None:
    assert normalizar_ids_bdns([9, 3, 9, 1, 3]) == [9, 3, 1]


def test_normalizar_lista_vacia() -> None:
    assert normalizar_ids_bdns([]) == []


def test_el_schema_aplica_la_normalizacion() -> None:
    assert AlertaCreate(nombre="x", regiones=[9, 9]).regiones == [9]
    assert AlertaUpdate(organos=[5, 5, 2]).organos == [5, 2]


@pytest.mark.parametrize("valor", [0, -1, 2**31, "andalucia"])
def test_el_schema_rechaza_ids_no_validos(valor: object) -> None:
    with pytest.raises(ValidationError):
        AlertaCreate(nombre="x", regiones=[valor])


def test_el_schema_limita_el_numero_de_filtros() -> None:
    with pytest.raises(ValidationError):
        AlertaCreate(nombre="x", organos=list(range(1, MAX_FILTROS + 2)))


# --- POST ---------------------------------------------------------------


async def test_crear_alerta_persiste_los_filtros_normalizados(
    client_gestor: AsyncClient, gestor: Sesion
) -> None:
    creada = await _crear(
        client_gestor,
        texto_busqueda="digitalización",
        nivel_administracion="ccaa",
        fecha_desde="2026-01-01",
        fecha_hasta="2026-12-31",
        frecuencia="semanal",
        organos=[1500, 1500, 42],
        regiones=[9, 9],
    )
    assert creada["usuario_id"] == gestor.id
    assert creada["frecuencia"] == "semanal"
    assert creada["organos"] == [1500, 42]
    assert creada["regiones"] == [9]

    # Lo que cuenta es lo guardado: una fila por id, sin duplicados.
    assert await _filtros_en_bd(creada["id"]) == ([1500, 42], [9])
    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, creada["id"])
    assert alerta is not None
    assert alerta.created_by == gestor.id


async def test_crear_alerta_con_valores_por_defecto(client_gestor: AsyncClient) -> None:
    creada = await _crear(client_gestor)
    assert creada["frecuencia"] == "diaria"
    assert creada["canal_notificacion"] == "email"
    assert creada["activa"] is True
    assert creada["solo_mrr"] is False
    assert creada["organos"] == []
    assert creada["regiones"] == []


@pytest.mark.parametrize("campo", ["organos", "regiones"])
@pytest.mark.parametrize("valor", [0, -1, "andalucia"])
async def test_crear_alerta_con_filtro_invalido(
    client_gestor: AsyncClient, campo: str, valor: object
) -> None:
    respuesta = await client_gestor.post("/alertas", json=_alerta(**{campo: [valor]}))
    assert respuesta.status_code == 422
    # El error señala el campo y el valor recibido.
    error = respuesta.json()["detail"][0]
    assert error["loc"][:2] == ["body", campo]
    assert error["input"] == valor


async def test_crear_alerta_con_rango_de_fechas_invertido(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.post(
        "/alertas", json=_alerta(fecha_desde="2026-12-31", fecha_hasta="2026-01-01")
    )
    assert respuesta.status_code == 422
    assert "fecha_desde" in respuesta.text


async def test_crear_alerta_con_frecuencia_invalida(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.post("/alertas", json=_alerta(frecuencia="horaria"))
    assert respuesta.status_code == 422


# --- PATCH --------------------------------------------------------------


async def test_patch_parcial_solo_toca_lo_enviado(client_gestor: AsyncClient) -> None:
    creada = await _crear(client_gestor, texto_busqueda="pymes", organos=[42], regiones=[9])

    respuesta = await client_gestor.patch(f"/alertas/{creada['id']}", json={"nombre": "Renombrada"})
    assert respuesta.status_code == 200, respuesta.text
    editada = respuesta.json()
    assert editada["nombre"] == "Renombrada"
    assert editada["texto_busqueda"] == "pymes"
    assert editada["organos"] == [42]
    assert editada["regiones"] == [9]
    assert datetime.fromisoformat(editada["updated_at"]) > datetime.fromisoformat(creada["updated_at"])


async def test_patch_de_filtros_reemplaza_la_lista(client_gestor: AsyncClient) -> None:
    creada = await _crear(client_gestor, organos=[42], regiones=[9, 1])

    respuesta = await client_gestor.patch(f"/alertas/{creada['id']}", json={"regiones": [4, 4, 13]})
    assert respuesta.status_code == 200, respuesta.text
    editada = respuesta.json()
    assert editada["regiones"] == [4, 13]
    assert editada["organos"] == [42]  # no venía: no se toca
    # Solo cambian filas hijas, y aun así la alerta queda firmada como editada.
    assert datetime.fromisoformat(editada["updated_at"]) > datetime.fromisoformat(creada["updated_at"])
    assert await _filtros_en_bd(creada["id"]) == ([42], [4, 13])


async def test_patch_con_lista_vacia_quita_los_filtros(client_gestor: AsyncClient) -> None:
    creada = await _crear(client_gestor, organos=[42], regiones=[9])

    respuesta = await client_gestor.patch(f"/alertas/{creada['id']}", json={"organos": []})
    assert respuesta.json()["organos"] == []
    assert await _filtros_en_bd(creada["id"]) == ([], [9])


@pytest.mark.parametrize("campo", ["nombre", "frecuencia", "activa", "regiones"])
async def test_patch_no_admite_null_en_campos_obligatorios(client_gestor: AsyncClient, campo: str) -> None:
    creada = await _crear(client_gestor)
    respuesta = await client_gestor.patch(f"/alertas/{creada['id']}", json={campo: None})
    assert respuesta.status_code == 422
    assert respuesta.json()["detail"][0]["loc"] == ["body", campo]


async def test_patch_permite_vaciar_campos_opcionales(client_gestor: AsyncClient) -> None:
    creada = await _crear(client_gestor, texto_busqueda="pymes", fecha_desde="2026-01-01")
    respuesta = await client_gestor.patch(
        f"/alertas/{creada['id']}", json={"texto_busqueda": None, "fecha_desde": None}
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["texto_busqueda"] is None
    assert respuesta.json()["fecha_desde"] is None


async def test_patch_valida_el_rango_contra_lo_ya_guardado(client_gestor: AsyncClient) -> None:
    """El body solo trae fecha_desde: el conflicto está con la fecha_hasta guardada."""
    creada = await _crear(client_gestor, fecha_desde="2026-01-01", fecha_hasta="2026-06-30")
    respuesta = await client_gestor.patch(f"/alertas/{creada['id']}", json={"fecha_desde": "2026-07-01"})
    assert respuesta.status_code == 422
    assert "fecha_hasta" in respuesta.json()["detail"]


async def test_patch_con_filtro_invalido(client_gestor: AsyncClient) -> None:
    creada = await _crear(client_gestor, regiones=[9])
    respuesta = await client_gestor.patch(f"/alertas/{creada['id']}", json={"regiones": ["andalucia"]})
    assert respuesta.status_code == 422
    assert await _filtros_en_bd(creada["id"]) == ([], [9])


async def test_patch_sobre_alerta_inexistente(client_gestor: AsyncClient) -> None:
    respuesta = await client_gestor.patch(f"/alertas/{ID_INEXISTENTE}", json={"nombre": "x"})
    assert respuesta.status_code == 404


# --- DELETE -------------------------------------------------------------


async def test_eliminar_alerta_borra_la_fila_y_sus_filtros(client_gestor: AsyncClient) -> None:
    creada = await _crear(client_gestor, organos=[42], regiones=[9])

    respuesta = await client_gestor.delete(f"/alertas/{creada['id']}")
    assert respuesta.status_code == 204
    assert respuesta.content == b""

    async with AsyncSessionLocal() as db:
        assert await db.get(Alerta, creada["id"]) is None
    assert await _filtros_en_bd(creada["id"]) == ([], [])

    # Y un segundo DELETE ya no la encuentra.
    assert (await client_gestor.delete(f"/alertas/{creada['id']}")).status_code == 404


async def test_eliminar_alerta_inexistente(client_gestor: AsyncClient) -> None:
    assert (await client_gestor.delete(f"/alertas/{ID_INEXISTENTE}")).status_code == 404


# --- Permisos -----------------------------------------------------------


async def test_un_companero_no_toca_la_alerta_de_otro(client_gestor: AsyncClient, empresa: dict) -> None:
    """404 y no 403: no se confirma que exista una alerta ajena."""
    creada = await _crear(client_gestor, regiones=[9])
    companero = await crear_sesion_en_bd(empresa["id"], "usuario")

    editar = await client_gestor.patch(
        f"/alertas/{creada['id']}", json={"nombre": "Mía ahora"}, headers=companero.headers
    )
    borrar = await client_gestor.delete(f"/alertas/{creada['id']}", headers=companero.headers)
    assert editar.status_code == 404
    assert borrar.status_code == 404

    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, creada["id"])
    assert alerta is not None
    assert alerta.nombre == creada["nombre"]


async def test_ni_el_admin_toca_la_alerta_de_otro(
    client_gestor: AsyncClient, client_admin: AsyncClient
) -> None:
    """Las alertas son personales: el rol admin no da acceso a ellas."""
    creada = await _crear(client_gestor)
    assert (await client_admin.patch(f"/alertas/{creada['id']}", json={"nombre": "x"})).status_code == 404
    assert (await client_admin.delete(f"/alertas/{creada['id']}")).status_code == 404


async def test_alertas_exige_token(client: AsyncClient) -> None:
    assert (await client.post("/alertas", json=_alerta())).status_code == 401
    assert (await client.patch("/alertas/1", json={"nombre": "x"})).status_code == 401
    assert (await client.delete("/alertas/1")).status_code == 401


# --- Validaciones de campo por HTTP -------------------------------------


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("nombre", ""),
        ("nombre", "x" * 151),
        ("texto_busqueda", "x" * 301),
        ("nivel_administracion", "galactico"),
        ("canal_notificacion", "paloma_mensajera"),
        ("organos", list(range(1, MAX_FILTROS + 2))),
    ],
)
async def test_crear_alerta_con_campo_invalido(
    client_gestor: AsyncClient, campo: str, valor: object
) -> None:
    """Los límites del schema también se cumplen entrando por la API, no solo
    al construir el modelo Pydantic a mano."""
    respuesta = await client_gestor.post("/alertas", json=_alerta(**{campo: valor}))
    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json()["detail"][0]["loc"][:2] == ["body", campo]


async def test_crear_alerta_en_el_limite_de_los_campos(client_gestor: AsyncClient) -> None:
    """La frontera de lo válido: 150 caracteres y MAX_FILTROS órganos entran."""
    creada = await _crear(
        client_gestor,
        nombre="x" * 150,
        texto_busqueda="y" * 300,
        organos=list(range(1, MAX_FILTROS + 1)),
    )
    assert len(creada["nombre"]) == 150
    assert len(creada["organos"]) == MAX_FILTROS


# --- Eliminar: el histórico de ejecuciones ------------------------------


async def test_eliminar_alerta_se_lleva_su_historial(client_gestor: AsyncClient) -> None:
    """El borrado es físico y arrastra `alerta_ejecucion` y su tabla
    intermedia (dos niveles de cascada). La convocatoria cacheada, en cambio,
    sobrevive: es compartida, igual que en favoritos."""
    creada = await _crear(client_gestor)
    codigo = codigo_bdns_de_prueba()
    async with AsyncSessionLocal() as db:
        convocatoria = Convocatoria(codigo_bdns=codigo, titulo="Detectada por la alerta")
        db.add(convocatoria)
        ejecuciones = [
            AlertaEjecucion(alerta_id=creada["id"], estado_envio=estado, convocatorias_encontradas=n)
            for estado, n in (("enviado", 1), ("sin_novedades", 0))
        ]
        db.add_all(ejecuciones)
        await db.flush()
        db.add(
            AlertaEjecucionConvocatoria(
                alerta_ejecucion_id=ejecuciones[0].id, convocatoria_id=convocatoria.id
            )
        )
        await db.commit()
        ids_ejecucion = [ejecucion.id for ejecucion in ejecuciones]

    assert (await client_gestor.delete(f"/alertas/{creada['id']}")).status_code == 204

    async with AsyncSessionLocal() as db:
        quedan = await db.scalars(
            select(AlertaEjecucion.id).where(AlertaEjecucion.alerta_id == creada["id"])
        )
        quedan_convocatorias = await db.scalars(
            select(AlertaEjecucionConvocatoria.id).where(
                AlertaEjecucionConvocatoria.alerta_ejecucion_id.in_(ids_ejecucion)
            )
        )
        sigue_cacheada = await db.scalar(
            select(Convocatoria.id).where(Convocatoria.codigo_bdns == codigo)
        )
    assert list(quedan) == []
    assert list(quedan_convocatorias) == []
    assert sigue_cacheada is not None


# --- El rol usuario ----------------------------------------------------


async def test_un_usuario_raso_gestiona_sus_propias_alertas(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    """Las alertas solo exigen estar autenticado, no ser gestor ni admin: son
    personales. Si alguien pusiera aquí GestorDep, este test se pondría rojo."""
    creada = await _crear(client_usuario, regiones=[9])
    assert creada["usuario_id"] == usuario_raso.id

    listado = await client_usuario.get("/alertas")
    assert [item["id"] for item in listado.json()["items"]] == [creada["id"]]

    editada = await client_usuario.patch(f"/alertas/{creada['id']}", json={"activa": False})
    assert editada.status_code == 200
    assert editada.json()["activa"] is False
    assert (await client_usuario.delete(f"/alertas/{creada['id']}")).status_code == 204


async def test_un_usuario_raso_no_alcanza_la_alerta_de_un_companero(
    client_usuario: AsyncClient, client_gestor: AsyncClient
) -> None:
    """Los dos comparten empresa (misma fixture), y aun así no se ven las
    alertas: cuelgan del usuario, no de la empresa."""
    ajena = await _crear(client_gestor)

    assert (await client_usuario.get("/alertas")).json()["total"] == 0
    assert (await client_usuario.get(f"/alertas/{ajena['id']}")).status_code == 404
    assert (await client_usuario.patch(f"/alertas/{ajena['id']}", json={"nombre": "x"})).status_code == 404
    assert (await client_usuario.delete(f"/alertas/{ajena['id']}")).status_code == 404


# --- El propietario no se secuestra por el body -------------------------


async def test_el_body_no_puede_asignar_la_alerta_a_otro(
    client_gestor: AsyncClient, gestor: Sesion, empresa: dict
) -> None:
    """`usuario_id` no está en AlertaCreate/AlertaUpdate: el propietario sale
    siempre del token. Mandarlo en el body no lo cambia ni al crear ni al
    editar, y la alerta no aparece en el listado del otro usuario."""
    otro = await crear_sesion_en_bd(empresa["id"], "usuario")

    creada = await _crear(client_gestor, usuario_id=otro.id)
    assert creada["usuario_id"] == gestor.id

    editada = await client_gestor.patch(
        f"/alertas/{creada['id']}", json={"nombre": "Sigue siendo mía", "usuario_id": otro.id, "id": 999}
    )
    assert editada.status_code == 200
    assert editada.json()["id"] == creada["id"]
    assert editada.json()["usuario_id"] == gestor.id

    async with AsyncSessionLocal() as db:
        alerta = await db.get(Alerta, creada["id"])
    assert alerta is not None
    assert alerta.usuario_id == gestor.id
    # La edición queda firmada por quien la hizo.
    assert alerta.updated_by == gestor.id

    del_otro = await client_gestor.get("/alertas", headers=otro.headers)
    assert del_otro.json()["total"] == 0
