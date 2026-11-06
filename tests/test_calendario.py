"""Calendar and ICS subscription (plan N6)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.main import app
from app.models import Usuario
from app.services.ics import EventoIcs, construir_ics, escapar, plegar
from tests.conftest import Sesion, crear_convocatoria_en_bd, crear_sesion_en_bd
from tests.migraciones import base_temporal

HOY = date.today()


def _en(dias: int) -> date:
    return HOY + timedelta(days=dias)


async def _favorita(c: AsyncClient, titulo: str, *, fin: date | None, inicio: date | None = None) -> str:
    conv = await crear_convocatoria_en_bd(titulo, fecha_fin_solicitud=fin, fecha_inicio_solicitud=inicio)
    r = await c.post("/favoritos", json={"convocatoria": {"codigo_bdns": conv.codigo_bdns, "titulo": titulo}})
    assert r.status_code == 201
    return conv.codigo_bdns


async def _seguida(c: AsyncClient, titulo: str, *, fin: date | None) -> dict:
    conv = await crear_convocatoria_en_bd(titulo, fecha_fin_solicitud=fin)
    r = await c.post("/seguimiento", json={"codigo_bdns": conv.codigo_bdns, "titulo": titulo})
    assert r.status_code == 201
    return r.json()


def _rango(dias: int = 60) -> dict:
    return {"desde": _en(-5).isoformat(), "hasta": _en(dias).isoformat()}


def _ruta_del_feed(url: str) -> str:
    return url[url.index("/calendario/feed/") :]


# --- ICS writer (pure) -------------------------------------------------------------------------------------


def test_escape_neutralises_line_breaks_and_separators() -> None:
    assert escapar("a,b;c\\d\r\ne\nf\rg") == "a\\,b\\;c\\\\d\\ne\\nf\\ng"


def test_a_title_cannot_inject_properties_or_events() -> None:
    malo = EventoIcs("x-1", HOY, "Hola\r\nBEGIN:VEVENT\r\nSUMMARY:falso", "cierre")
    ics = construir_ics([malo], ahora=datetime(2026, 1, 1, tzinfo=UTC))
    lineas = ics.split("\r\n")
    assert lineas.count("BEGIN:VEVENT") == 1
    assert "SUMMARY:falso" not in lineas


def test_lines_fold_at_75_octets_without_splitting_characters() -> None:
    linea = "SUMMARY:" + "ñ" * 60
    for parte in plegar(linea).split("\r\n"):
        assert len(parte.encode("utf-8")) <= 75
    assert plegar(linea).replace("\r\n ", "") == linea
    assert plegar("corta") == "corta"


def test_ics_structure_and_all_day_dates() -> None:
    evento = EventoIcs("fav-cierre-1", date(2026, 12, 31), "Ayuda", "cierre")
    ics = construir_ics([evento], ahora=datetime(2026, 1, 1, tzinfo=UTC))
    assert ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "UID:fav-cierre-1@subvfy\r\n" in ics and "DTSTAMP:20260101T000000Z\r\n" in ics
    assert "DTSTART;VALUE=DATE:20261231\r\n" in ics and "DTEND;VALUE=DATE:20270101\r\n" in ics
    assert "CATEGORIES:Cierre de plazo\r\n" in ics
    assert "\n" not in ics.replace("\r\n", "")


# --- /calendario -------------------------------------------------------------------------------------------


async def test_events_require_authentication(client: AsyncClient) -> None:
    assert (await client.get("/calendario", params=_rango())).status_code == 401
    assert (await client.get("/calendario/ics-token")).status_code == 401
    assert (await client.get("/calendario/export.ics")).status_code == 401


async def test_events_merge_favorites_board_and_tasks(client_gestor: AsyncClient) -> None:
    codigo = await _favorita(client_gestor, "Favorita", fin=_en(10), inicio=_en(3))
    item = await _seguida(client_gestor, "Seguida", fin=_en(20))
    await client_gestor.post(
        f"/seguimiento/{item['id']}/tareas", json={"titulo": "Memoria", "fecha_limite": _en(5).isoformat()}
    )
    r = await client_gestor.get("/calendario", params=_rango())
    assert r.status_code == 200
    resumen = [(e["fecha"], e["titulo"], e["tipo"], e["origen"]) for e in r.json()]
    assert resumen == [
        (_en(3).isoformat(), "Favorita", "apertura", "favoritos"),
        (_en(5).isoformat(), "Memoria", "tarea", "seguimiento"),
        (_en(10).isoformat(), "Favorita", "cierre", "favoritos"),
        (_en(20).isoformat(), "Seguida", "cierre", "seguimiento"),
    ]
    assert r.json()[0]["codigo_bdns"] == codigo and r.json()[1]["codigo_bdns"] == item["codigo_bdns"]
    assert len({e["id"] for e in r.json()}) == 4 and all(isinstance(e["id"], str) for e in r.json())


async def test_origin_filter_accepts_comma_and_repeated_values(client_gestor: AsyncClient) -> None:
    await _favorita(client_gestor, "F", fin=_en(10))
    await _seguida(client_gestor, "S", fin=_en(11))
    solo_fav = (await client_gestor.get("/calendario", params={**_rango(), "origen": "favoritos"})).json()
    assert {e["origen"] for e in solo_fav} == {"favoritos"}
    ambos = (await client_gestor.get("/calendario", params={**_rango(), "origen": "favoritos,seguimiento"})).json()
    assert {e["origen"] for e in ambos} == {"favoritos", "seguimiento"}
    r = _rango()
    consulta = f"desde={r['desde']}&hasta={r['hasta']}&origen=seguimiento&origen=favoritos"
    rep = await client_gestor.get(f"/calendario?{consulta}")
    assert {e["origen"] for e in rep.json()} == {"favoritos", "seguimiento"}
    assert (await client_gestor.get("/calendario", params={**_rango(), "origen": "otro"})).status_code == 422


async def test_range_is_inclusive_and_validated(client_gestor: AsyncClient) -> None:
    await _favorita(client_gestor, "Borde", fin=_en(7))
    justo = {"desde": _en(7).isoformat(), "hasta": _en(7).isoformat()}
    assert len((await client_gestor.get("/calendario", params=justo)).json()) == 1
    fuera = {"desde": _en(8).isoformat(), "hasta": _en(9).isoformat()}
    assert (await client_gestor.get("/calendario", params=fuera)).json() == []
    inverso = {"desde": _en(9).isoformat(), "hasta": _en(8).isoformat()}
    assert (await client_gestor.get("/calendario", params=inverso)).status_code == 422
    enorme = {"desde": _en(0).isoformat(), "hasta": _en(401).isoformat()}
    assert (await client_gestor.get("/calendario", params=enorme)).status_code == 422
    assert (await client_gestor.get("/calendario", params={"desde": "ayer", "hasta": "hoy"})).status_code == 422
    assert (await client_gestor.get("/calendario")).status_code == 422


async def test_favorites_are_personal_but_the_board_belongs_to_the_company(
    client_gestor: AsyncClient, client_usuario: AsyncClient
) -> None:
    await _favorita(client_gestor, "Solo del gestor", fin=_en(10))
    await _seguida(client_gestor, "De la empresa", fin=_en(11))
    visto = (await client_usuario.get("/calendario", params=_rango())).json()
    assert [(e["titulo"], e["origen"]) for e in visto] == [("De la empresa", "seguimiento")]


async def test_no_leak_between_companies(client_gestor: AsyncClient, otra_empresa: dict) -> None:
    await _favorita(client_gestor, "F", fin=_en(10))
    await _seguida(client_gestor, "S", fin=_en(11))
    ajeno = await crear_sesion_en_bd(otra_empresa["id"], "admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=ajeno.headers) as c:
        assert (await c.get("/calendario", params=_rango())).json() == []


# --- export and subscription -------------------------------------------------------------------------------


async def test_export_is_an_attachment_with_the_events(client_gestor: AsyncClient) -> None:
    await _favorita(client_gestor, "Mi favorita", fin=_en(10))
    r = await client_gestor.get("/calendario/export.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"]
    assert "SUMMARY:Mi favorita" in r.text and r.text.count("BEGIN:VEVENT") == 1
    solo_seg = await client_gestor.get("/calendario/export.ics", params={"origen": "seguimiento"})
    assert "BEGIN:VEVENT" not in solo_seg.text


async def test_token_is_created_once_unguessable_and_the_url_points_at_the_api(
    client_gestor: AsyncClient, gestor: Sesion
) -> None:
    primero = (await client_gestor.get("/calendario/ics-token")).json()["url"]
    assert primero == (await client_gestor.get("/calendario/ics-token")).json()["url"]
    assert "/calendario/feed/" in primero and primero.endswith(".ics")
    token = primero.rsplit("/", 1)[1].removesuffix(".ics")
    assert len(token) >= 43 and token.replace("-", "").replace("_", "").isalnum()
    otro = await crear_sesion_en_bd(gestor.empresa_id, "usuario")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=otro.headers) as c:
        assert (await c.get("/calendario/ics-token")).json()["url"] != primero


async def test_feed_is_public_by_token_and_the_token_is_not_in_the_profile(
    client_gestor: AsyncClient, client: AsyncClient
) -> None:
    await _favorita(client_gestor, "Para el feed", fin=_en(10))
    url = (await client_gestor.get("/calendario/ics-token")).json()["url"]
    r = await client.get(_ruta_del_feed(url))  # no Authorization header
    assert r.status_code == 200 and "SUMMARY:Para el feed" in r.text
    assert r.headers["content-type"].startswith("text/calendar") and "noindex" in r.headers["x-robots-tag"]
    token = url.rsplit("/", 1)[1].removesuffix(".ics")
    assert token not in (await client_gestor.get("/auth/me")).text


async def test_rotation_invalidates_the_old_url(client_gestor: AsyncClient, client: AsyncClient) -> None:
    vieja = (await client_gestor.get("/calendario/ics-token")).json()["url"]
    nueva = (await client_gestor.post("/calendario/ics-token/rotar")).json()["url"]
    assert vieja != nueva
    assert (await client.get(_ruta_del_feed(vieja))).status_code == 404
    assert (await client.get(_ruta_del_feed(nueva))).status_code == 200
    assert (await client_gestor.get("/calendario/ics-token")).json()["url"] == nueva


@pytest.mark.parametrize(
    "ruta",
    ["x.ics", "a" * 43 + ".ics", "a" * 43, ".ics", "a" * 200 + ".ics", "..%2Fetc%2Fpasswd.ics", "a" * 43 + ".txt"],
)
async def test_feed_unknown_or_malformed_token_is_404(client: AsyncClient, ruta: str) -> None:
    assert (await client.get(f"/calendario/feed/{ruta}")).status_code == 404


async def test_feed_stops_when_the_account_cannot_operate(
    client_gestor: AsyncClient, client: AsyncClient, gestor: Sesion
) -> None:
    url = (await client_gestor.get("/calendario/ics-token")).json()["url"]
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE usuario SET estado = 'bloqueado' WHERE id = :i"), {"i": gestor.id})
        await db.commit()
    assert (await client.get(_ruta_del_feed(url))).status_code == 404


async def test_db_refuses_short_tokens_and_duplicates(gestor: Sesion, usuario_raso: Sesion) -> None:
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="ck_usuario_calendario_token"):
            await db.execute(text("UPDATE usuario SET calendario_token = 'corto' WHERE id = :i"), {"i": gestor.id})
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE usuario SET calendario_token = :t WHERE id = :i"), {"t": "z" * 40, "i": gestor.id}
        )
        with pytest.raises(IntegrityError, match="uq_usuario_calendario_token"):
            await db.execute(
                text("UPDATE usuario SET calendario_token = :t WHERE id = :i"), {"t": "z" * 40, "i": usuario_raso.id}
            )


async def test_token_is_stored_for_the_owner(client_gestor: AsyncClient, gestor: Sesion) -> None:
    await client_gestor.get("/calendario/ics-token")
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(Usuario.calendario_token).where(Usuario.id == gestor.id))).scalar_one()


async def test_migration_up_and_down() -> None:
    consulta = text("SELECT 1 FROM information_schema.columns WHERE column_name = 'calendario_token'")
    async with base_temporal() as base:
        base.alembic("upgrade", "head")
        async with base.motor.connect() as c:
            assert (await c.execute(consulta)).first()
        base.alembic("downgrade", "a8e2c5d91f47")
        async with base.motor.connect() as c:
            assert not (await c.execute(consulta)).first()
