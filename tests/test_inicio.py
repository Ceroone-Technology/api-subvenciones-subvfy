"""`GET /inicio/resumen` (plan N10) and the previous-access bookkeeping of the login.

The database is shared with real grants, so the grant counters are asserted as deltas against a baseline taken
with the same user; the per-user and per-company counters (alerts, board) start from zero for a fresh user.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.models import AlertaEjecucion, Seguimiento, Usuario
from app.services.convocatoria_estado import hoy_madrid
from tests.conftest import (
    PASSWORD_TEST,
    Sesion,
    crear_alerta_en_bd,
    crear_convocatoria_en_bd,
    crear_empresa_en_bd,
    crear_sesion_en_bd,
)

CAMPOS = {"novedades", "cierran_7_dias", "alertas_con_resultados", "en_seguimiento", "novedades_desde"}


async def _hoy():  # noqa: ANN202
    async with AsyncSessionLocal() as db:
        return await db.scalar(select(hoy_madrid()))


async def _resumen(cliente: AsyncClient) -> dict:
    r = await cliente.get("/inicio/resumen")
    assert r.status_code == 200, r.text
    return r.json()


async def _convocatoria(dias_fin: int, *, visto_hace_dias: float = 0) -> str:
    hoy = await _hoy()
    c = await crear_convocatoria_en_bd(
        "Ayudas inicio",
        fecha_inicio_solicitud=hoy - timedelta(days=30),
        fecha_fin_solicitud=hoy + timedelta(days=dias_fin),
        first_seen_at=datetime.now(UTC) - timedelta(days=visto_hace_dias),
    )
    return c.codigo_bdns


async def _ejecucion(alerta_id: int, usuario_id: int, hace_horas: float, encontradas: int) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            AlertaEjecucion(
                alerta_id=alerta_id,
                fecha_ejecucion_at=datetime.now(UTC) - timedelta(hours=hace_horas),
                convocatorias_encontradas=encontradas,
                estado_envio="enviado",
                created_by=usuario_id,
                updated_by=usuario_id,
            )
        )
        await db.commit()


async def _fijar_acceso(usuario_id: int, *, actual: datetime | None, previo: datetime | None) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Usuario)
            .where(Usuario.id == usuario_id)
            .values(ultimo_acceso_at=actual, ultimo_acceso_previo_at=previo)
        )
        await db.commit()


async def _accesos(usuario_id: int) -> tuple[datetime | None, datetime | None]:
    async with AsyncSessionLocal() as db:
        consulta = select(Usuario.ultimo_acceso_at, Usuario.ultimo_acceso_previo_at).where(Usuario.id == usuario_id)
        fila = (await db.execute(consulta)).one()
        return fila[0], fila[1]


async def test_requiere_login(client: AsyncClient) -> None:
    assert (await client.get("/inicio/resumen")).status_code == 401


async def test_forma_y_ceros_para_un_usuario_nuevo(client_usuario: AsyncClient) -> None:
    datos = await _resumen(client_usuario)
    assert set(datos) == CAMPOS
    assert datos["alertas_con_resultados"] == 0 and datos["en_seguimiento"] == 0


async def test_novedades_y_cierres_respetan_ventana_estado_y_descartes(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    await _fijar_acceso(usuario_raso.id, actual=datetime.now(UTC), previo=datetime.now(UTC) - timedelta(hours=1))
    antes = await _resumen(client_usuario)

    cierra_pronto = await _convocatoria(3)
    lejana = await _convocatoria(20)
    await _convocatoria(-2)  # already closed: neither news nor closing
    await _convocatoria(20, visto_hace_dias=10)  # seen before the previous access: not news
    await _convocatoria(7)  # the 7th day is inclusive

    despues = await _resumen(client_usuario)
    assert despues["novedades"] - antes["novedades"] == 3
    assert despues["cierran_7_dias"] - antes["cierran_7_dias"] == 2

    # A discard hides it from both counters, for this user only.
    assert (await client_usuario.post("/descartes", json={"codigo_bdns": cierra_pronto})).status_code == 201
    assert (await client_usuario.post("/descartes", json={"codigo_bdns": lejana})).status_code == 201
    final = await _resumen(client_usuario)
    assert final["novedades"] - antes["novedades"] == 1
    assert final["cierran_7_dias"] - antes["cierran_7_dias"] == 1


async def test_sin_acceso_previo_la_ventana_es_de_siete_dias(client_usuario: AsyncClient, usuario_raso: Sesion) -> None:
    await _fijar_acceso(usuario_raso.id, actual=None, previo=None)
    antes = await _resumen(client_usuario)
    await _convocatoria(20, visto_hace_dias=3)
    await _convocatoria(20, visto_hace_dias=9)
    despues = await _resumen(client_usuario)
    assert despues["novedades"] - antes["novedades"] == 1
    desde = datetime.fromisoformat(despues["novedades_desde"])
    assert timedelta(days=6, hours=23) < datetime.now(UTC) - desde < timedelta(days=7, hours=1)


async def test_un_acceso_previo_muy_antiguo_se_limita_a_treinta_dias(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    await _fijar_acceso(usuario_raso.id, actual=datetime.now(UTC), previo=datetime.now(UTC) - timedelta(days=400))
    desde = datetime.fromisoformat((await _resumen(client_usuario))["novedades_desde"])
    assert timedelta(days=29, hours=23) < datetime.now(UTC) - desde < timedelta(days=30, hours=1)


async def test_alertas_con_resultados_cuenta_activas_con_la_ultima_ejecucion_con_novedades(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    usuario_raso_otro = await crear_sesion_en_bd(usuario_raso.empresa_id, "usuario")
    con = await crear_alerta_en_bd(usuario_raso.id, nombre="Con resultados")
    await _ejecucion(con, usuario_raso.id, 30, 0)
    await _ejecucion(con, usuario_raso.id, 5, 4)
    sin = await crear_alerta_en_bd(usuario_raso.id, nombre="Ya sin novedades")
    await _ejecucion(sin, usuario_raso.id, 30, 6)
    await _ejecucion(sin, usuario_raso.id, 5, 0)
    pausada = await crear_alerta_en_bd(usuario_raso.id, nombre="Pausada", activa=False)
    await _ejecucion(pausada, usuario_raso.id, 5, 3)
    ajena = await crear_alerta_en_bd(usuario_raso_otro.id, nombre="De otro")
    await _ejecucion(ajena, usuario_raso_otro.id, 5, 3)

    assert (await _resumen(client_usuario))["alertas_con_resultados"] == 1


async def test_en_seguimiento_cuenta_las_tarjetas_no_resueltas_de_mi_empresa(
    client_usuario: AsyncClient, usuario_raso: Sesion
) -> None:
    otra = await crear_empresa_en_bd("Otra empresa inicio SL")
    async with AsyncSessionLocal() as db:
        for empresa_id, estado in (
            (usuario_raso.empresa_id, "interesa"),
            (usuario_raso.empresa_id, "preparando"),
            (usuario_raso.empresa_id, "presentada"),
            (usuario_raso.empresa_id, "resuelta"),
            (otra["id"], "interesa"),
        ):
            c = await crear_convocatoria_en_bd("Seguimiento inicio")
            db.add(
                Seguimiento(
                    empresa_id=empresa_id, convocatoria_id=c.id, estado=estado,
                    resultado="concedida" if estado == "resuelta" else None,
                    created_by=usuario_raso.id, updated_by=usuario_raso.id,
                )  # fmt: skip
            )
        await db.commit()
    assert (await _resumen(client_usuario))["en_seguimiento"] == 3


async def test_login_guarda_el_acceso_previo_solo_al_empezar_una_sesion_nueva(
    client: AsyncClient, empresa: dict
) -> None:
    sesion = await crear_sesion_en_bd(empresa["id"], "usuario")
    credenciales = {"email": sesion.email, "password": PASSWORD_TEST}

    # First ever login: nothing to remember.
    assert (await client.post("/auth/login", json=credenciales)).status_code == 200
    primero, previo = await _accesos(sesion.id)
    assert primero is not None and previo is None

    # A second login minutes later is the same session: previous access stays empty.
    assert (await client.post("/auth/login", json=credenciales)).status_code == 200
    assert (await _accesos(sesion.id))[1] is None

    # After a long absence the last access becomes the previous one.
    hace = datetime.now(UTC) - timedelta(hours=5)
    await _fijar_acceso(sesion.id, actual=hace, previo=None)
    assert (await client.post("/auth/login", json=credenciales)).status_code == 200
    actual, previo = await _accesos(sesion.id)
    assert previo == hace and actual is not None and actual > hace

    # And one more quick login keeps it.
    assert (await client.post("/auth/login", json=credenciales)).status_code == 200
    assert (await _accesos(sesion.id))[1] == hace
