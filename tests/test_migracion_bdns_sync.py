"""Schema of the BDNS local sync (PR1 of `bdns-local-sync`).

Inspects the real database (PostgreSQL catalogs), not the models: what matters
is what the hand-written migration `bdns_local_sync` actually created. Alembic
does not compare CHECKs and the audit FKs are added by hand, so the catalog
is the only source of truth for them.

Rows created here follow the teardown conventions of `conftest.py`:
`catalogo_bdns.descripcion` and `sync_run.error_detalle` start with
`TEST-`, and test users sign what they write.
"""

import warnings
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.database import AsyncSessionLocal, Base, engine
from app.models import CatalogoBdns, SyncRun
from app.services.sistema import id_usuario_sistema
from tests.conftest import (
    MARCA_TEST,
    Sesion,
    crear_convocatoria_en_bd,
)

# column -> (udt_name, is_nullable, fragment expected in column_default or None)
COLUMNAS_CONVOCATORIA = {
    "bdns_id": ("int4", "YES", None),
    "fecha_inicio_solicitud": ("date", "YES", None),
    "fecha_fin_solicitud": ("date", "YES", None),
    "abierto_bdns": ("bool", "YES", None),
    "plazo_permanente": ("bool", "NO", "false"),
    "presupuesto_total": ("numeric", "YES", None),
    "tipo_convocatoria": ("varchar", "YES", None),
    "finalidad": ("varchar", "YES", None),
    "url_bases_reguladoras": ("varchar", "YES", None),
    "sede_electronica": ("varchar", "YES", None),
    "organo_ids": ("_int4", "NO", "'{}'"),
    "region_ids": ("_int4", "NO", "'{}'"),
    "beneficiarios": ("_text", "NO", "'{}'"),
    "sectores": ("_text", "NO", "'{}'"),
    "detalle": ("jsonb", "YES", None),
    "contenido_hash": ("bpchar", "YES", None),
    "first_seen_at": ("timestamptz", "YES", None),
    "disponible_at": ("timestamptz", "YES", None),
    "last_seen_at": ("timestamptz", "YES", None),
    "detalle_sync_at": ("timestamptz", "YES", None),
    "removed_at": ("timestamptz", "YES", None),
    "busqueda": ("tsvector", "YES", None),
}

# index -> fragments that must appear in `pg_indexes.indexdef`
INDICES_CONVOCATORIA = {
    "ix_convocatoria_registro": ["btree", "fecha_registro DESC, id DESC", "WHERE (removed_at IS NULL)"],
    "ix_convocatoria_fin": ["btree", "(fecha_fin_solicitud)"],
    "ix_convocatoria_inicio": ["btree", "(fecha_inicio_solicitud)"],
    "ix_convocatoria_busqueda": ["USING gin", "(busqueda)"],
    "ix_convocatoria_organo_ids": ["USING gin", "(organo_ids)"],
    "ix_convocatoria_region_ids": ["USING gin", "(region_ids)"],
    "ix_convocatoria_beneficiarios": ["USING gin", "(beneficiarios)"],
    "ix_convocatoria_refresco": ["btree", "(detalle_sync_at NULLS FIRST)"],
    "ix_convocatoria_disponible": ["btree", "(disponible_at)"],
    "ix_convocatoria_presupuesto": ["btree", "(presupuesto_total DESC NULLS LAST)"],
    "ix_sync_run_modo_estado": ["btree", "(modo, estado, finished_at DESC)"],
}


async def _filas(sql: str, **params) -> list[dict]:
    async with AsyncSessionLocal() as db:
        resultado = await db.execute(text(sql), params)
        return [dict(fila) for fila in resultado.mappings().all()]


async def _insertar(*objetos) -> None:
    async with AsyncSessionLocal() as db:
        db.add_all(objetos)
        await db.commit()


def _catalogo(bdns_id: int, **extra) -> CatalogoBdns:
    valores = {"tipo": "organo", "descripcion": f"{MARCA_TEST}organo {bdns_id}"} | extra
    return CatalogoBdns(bdns_id=bdns_id, **valores)


def _sync_run(**extra) -> SyncRun:
    valores = {"modo": "incremental", "error_detalle": f"{MARCA_TEST}sync"} | extra
    return SyncRun(**valores)


@pytest.mark.parametrize("columna", sorted(COLUMNAS_CONVOCATORIA))
async def test_convocatoria_has_the_sync_columns(columna: str) -> None:
    udt, nullable, default = COLUMNAS_CONVOCATORIA[columna]
    filas = await _filas(
        "SELECT udt_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'convocatoria' AND column_name = :c",
        c=columna,
    )
    assert len(filas) == 1, f"column {columna} is missing"
    assert (filas[0]["udt_name"], filas[0]["is_nullable"]) == (udt, nullable)
    if default is not None:
        assert default in filas[0]["column_default"]


async def test_convocatoria_column_sizes() -> None:
    filas = await _filas(
        "SELECT column_name, character_maximum_length AS largo, numeric_precision AS precision, "
        "numeric_scale AS escala FROM information_schema.columns WHERE table_name = 'convocatoria'"
    )
    por_columna = {fila["column_name"]: fila for fila in filas}
    assert (por_columna["presupuesto_total"]["precision"], por_columna["presupuesto_total"]["escala"]) == (16, 2)
    assert {c: por_columna[c]["largo"] for c in ("tipo_convocatoria", "finalidad", "contenido_hash")} == {
        "tipo_convocatoria": 100,
        "finalidad": 300,
        "contenido_hash": 64,
    }
    assert por_columna["url_bases_reguladoras"]["largo"] == por_columna["sede_electronica"]["largo"] == 1000


async def test_busqueda_is_a_stored_generated_column() -> None:
    filas = await _filas(
        "SELECT is_generated, generation_expression FROM information_schema.columns "
        "WHERE table_name = 'convocatoria' AND column_name = 'busqueda'"
    )
    assert filas[0]["is_generated"] == "ALWAYS"
    assert "es_unaccent" in filas[0]["generation_expression"]


@pytest.mark.parametrize("indice", sorted(INDICES_CONVOCATORIA))
async def test_indexes_exist_with_their_definition(indice: str) -> None:
    filas = await _filas("SELECT indexdef FROM pg_indexes WHERE indexname = :i", i=indice)
    assert len(filas) == 1, f"index {indice} is missing"
    for fragmento in INDICES_CONVOCATORIA[indice]:
        assert fragmento in filas[0]["indexdef"]


async def test_the_existing_convocatoria_check_is_untouched() -> None:
    filas = await _filas(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'convocatoria'::regclass AND contype = 'c'"
    )
    assert {fila["conname"] for fila in filas} == {"ck_convocatoria_nivel"}


@pytest.mark.parametrize(
    ("documento", "consulta", "coincide"),
    [
        ("Subvención para la innovación", "innovacion", True),  # accent only in the document
        ("Subvencion para la innovacion", "innovación", True),  # accent only in the query
        ("Ayudas a la digitalización", "ayuda", True),  # Spanish stemming kept (plural)
        ("Ayudas a la digitalización", "agricultura", False),  # unrelated word
    ],
)
async def test_es_unaccent_folds_accents_and_keeps_spanish_stemming(
    documento: str, consulta: str, coincide: bool
) -> None:
    (fila,) = await _filas(
        "SELECT to_tsvector('es_unaccent', :d) @@ plainto_tsquery('es_unaccent', :q) AS coincide",
        d=documento,
        q=consulta,
    )
    assert fila == {"coincide": coincide}


async def test_busqueda_is_filled_and_regenerated_by_the_database() -> None:
    creada = await crear_convocatoria_en_bd(
        "Ayudas a la innovación", organo_convocante="Ministerio de Industria", finalidad="Transformación digital"
    )

    async def coincidencias(consulta: str) -> bool:
        async with AsyncSessionLocal() as db:
            return bool(
                await db.scalar(
                    text("SELECT busqueda @@ plainto_tsquery('es_unaccent', :q) FROM convocatoria WHERE id = :id"),
                    {"q": consulta, "id": creada.id},
                )
            )

    assert await coincidencias("innovacion") is True  # title, no accent
    assert await coincidencias("industria") is True  # organ
    assert await coincidencias("transformacion") is True  # finalidad
    assert await coincidencias("agricultura") is False

    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE convocatoria SET titulo = 'Ayudas a la agricultura' WHERE id = :id"), {"id": creada.id}
        )
        await db.commit()
    assert await coincidencias("agricultura") is True
    assert await coincidencias("innovacion") is False


async def test_busqueda_weights_title_above_the_rest() -> None:
    creada = await crear_convocatoria_en_bd("Solar", organo_convocante="Eolica")
    (fila,) = await _filas("SELECT busqueda::text AS vector FROM convocatoria WHERE id = :id", id=creada.id)
    # Spanish stems; positions continue across the A and B parts.
    assert fila["vector"] == "'eolic':2B 'sol':1A"


async def test_the_database_rejects_writing_busqueda_directly() -> None:
    creada = await crear_convocatoria_en_bd("Ayudas")
    async with AsyncSessionLocal() as db:
        with pytest.raises(Exception, match="busqueda"):
            await db.execute(
                text("UPDATE convocatoria SET busqueda = to_tsvector('x') WHERE id = :id"), {"id": creada.id}
            )


async def test_a_minimal_convocatoria_gets_the_column_defaults() -> None:
    creada = await crear_convocatoria_en_bd("Ayudas a la digitalización")
    assert creada.plazo_permanente is False
    assert creada.organo_ids == []
    assert creada.region_ids == []
    assert creada.beneficiarios == []
    assert creada.sectores == []
    assert creada.detalle is None
    assert creada.removed_at is None


async def test_a_convocatoria_stores_the_sync_fields() -> None:
    creada = await crear_convocatoria_en_bd(
        "Ayudas a la innovación",
        bdns_id=123456,
        organo_ids=[1, 5, 9],
        region_ids=[8],
        beneficiarios=["PYME", "AUTONOMOS"],
        presupuesto_total=1500000.5,
        detalle={"documentos": [{"id": 7}]},
        contenido_hash="a" * 64,
    )
    assert (creada.bdns_id, creada.organo_ids, creada.region_ids) == (123456, [1, 5, 9], [8])
    assert creada.beneficiarios == ["PYME", "AUTONOMOS"]
    assert str(creada.presupuesto_total) == "1500000.50"
    assert creada.detalle == {"documentos": [{"id": 7}]}
    assert creada.contenido_hash == "a" * 64


@pytest.mark.parametrize(
    ("tabla", "modelo"),
    [
        ("catalogo_bdns", lambda: _catalogo(-1, tipo="otro")),
        ("catalogo_bdns", lambda: _catalogo(-2, nivel_administracion="otro")),
        ("sync_run", lambda: _sync_run(modo="otro")),
        ("sync_run", lambda: _sync_run(estado="otro")),
    ],
    ids=["catalogo-tipo", "catalogo-nivel", "sync-modo", "sync-estado"],
)
async def test_check_constraints_reject_unknown_values(tabla: str, modelo) -> None:
    with pytest.raises(IntegrityError, match="violates check constraint"):
        await _insertar(modelo())


async def test_check_constraints_accept_every_documented_value() -> None:
    catalogos = [_catalogo(-10, tipo="organo", nivel_administracion="ccaa"), _catalogo(-11, tipo="region")]
    catalogos += [
        _catalogo(-12 - i, nivel_administracion=nivel) for i, nivel in enumerate(("estado", "local", "otros"))
    ]
    runs = [
        _sync_run(modo=modo, estado=estado)
        for modo in ("incremental", "backfill")
        for estado in ("running", "ok", "partial", "failed")
    ]
    await _insertar(*catalogos, *runs)
    async with AsyncSessionLocal() as db:
        guardados = (await db.scalars(select(SyncRun).where(SyncRun.error_detalle == f"{MARCA_TEST}sync"))).all()
    assert len(guardados) == 8
    assert len({(r.modo, r.estado) for r in guardados}) == 8


async def test_catalogo_primary_key_is_tipo_and_bdns_id() -> None:
    await _insertar(_catalogo(-20, tipo="organo"))
    await _insertar(_catalogo(-20, tipo="region"))  # same id, other tipo: allowed
    with pytest.raises(IntegrityError, match="catalogo_bdns_pkey"):
        await _insertar(_catalogo(-20, tipo="organo"))


async def test_catalogo_stores_the_hierarchy_path() -> None:
    await _insertar(
        _catalogo(
            -30,
            padre_bdns_id=-29,
            codigo="ES12",
            nivel_administracion="ccaa",
            ruta_ids=[-1, -29, -30],
            ruta_norm="MINISTERIO DE INDUSTRIA > DIRECCION GENERAL",
        )
    )
    async with AsyncSessionLocal() as db:
        fila = await db.get(CatalogoBdns, ("organo", -30))
    assert fila is not None
    assert (fila.padre_bdns_id, fila.codigo, fila.ruta_ids) == (-29, "ES12", [-1, -29, -30])
    assert fila.ruta_norm == "MINISTERIO DE INDUSTRIA > DIRECCION GENERAL"
    assert fila.sincronizado_at is not None


async def test_catalogo_defaults_to_an_empty_path() -> None:
    await _insertar(_catalogo(-31))
    async with AsyncSessionLocal() as db:
        fila = await db.get(CatalogoBdns, ("organo", -31))
    assert fila is not None
    assert fila.ruta_ids == []


async def test_sync_run_defaults() -> None:
    await _insertar(_sync_run())
    async with AsyncSessionLocal() as db:
        fila = (await db.scalars(select(SyncRun).where(SyncRun.error_detalle == f"{MARCA_TEST}sync"))).one()
    assert fila.estado == "running"
    assert fila.started_at is not None
    assert fila.finished_at is None
    counters = (
        fila.listadas, fila.detalles, fila.insertadas, fila.actualizadas,
        fila.retiradas, fila.errores, fila.organos_sin_resolver, fila.peticiones_bdns,
    )  # fmt: skip
    assert counters == (0,) * 8
    assert fila.cursor is None


async def test_sync_run_stores_window_and_cursor() -> None:
    await _insertar(
        _sync_run(
            modo="backfill",
            estado="partial",
            ventana_desde=date(2026, 8, 1),
            ventana_hasta=date(2026, 8, 31),
            cursor={"mes": "2026-08", "pagina": 12},
        )
    )
    async with AsyncSessionLocal() as db:
        fila = (await db.scalars(select(SyncRun).where(SyncRun.error_detalle == f"{MARCA_TEST}sync"))).one()
    assert (fila.ventana_desde, fila.ventana_hasta) == (date(2026, 8, 1), date(2026, 8, 31))
    assert fila.cursor == {"mes": "2026-08", "pagina": 12}


@pytest.mark.parametrize("tabla", ["catalogo_bdns", "sync_run"])
async def test_audit_foreign_keys_exist_in_the_database(tabla: str) -> None:
    filas = await _filas(
        "SELECT conname, pg_get_constraintdef(oid) AS definicion FROM pg_constraint "
        "WHERE conrelid = CAST(:t AS regclass) AND contype = 'f'",
        t=tabla,
    )
    definiciones = {fila["conname"]: fila["definicion"] for fila in filas}
    assert definiciones == {
        f"fk_{tabla}_created_by": "FOREIGN KEY (created_by) REFERENCES usuario(id)",
        f"fk_{tabla}_updated_by": "FOREIGN KEY (updated_by) REFERENCES usuario(id)",
    }


async def test_sync_writes_can_be_signed_with_the_system_user() -> None:
    async with AsyncSessionLocal() as db:
        sistema = await id_usuario_sistema(db)
    await _insertar(
        _catalogo(-40, created_by=sistema, updated_by=sistema),
        _sync_run(created_by=sistema, updated_by=sistema),
    )
    async with AsyncSessionLocal() as db:
        catalogo = await db.get(CatalogoBdns, ("organo", -40))
        run = (await db.scalars(select(SyncRun).where(SyncRun.error_detalle == f"{MARCA_TEST}sync"))).one()
    assert catalogo is not None
    assert (catalogo.created_by, catalogo.updated_by) == (sistema, sistema)
    assert (run.created_by, run.updated_by) == (sistema, sistema)


async def test_rows_signed_by_a_test_user_do_not_block_the_teardown(usuario_raso: Sesion) -> None:
    """If `limpiar_datos_de_test` forgot these two tables, deleting the user
    would fail with an audit FK violation and this test would error."""
    await _insertar(
        _catalogo(-50, created_by=usuario_raso.id, updated_by=usuario_raso.id),
        _sync_run(created_by=usuario_raso.id, updated_by=usuario_raso.id),
    )
    async with AsyncSessionLocal() as db:
        firmadas = await db.scalar(text("SELECT count(*) FROM sync_run WHERE created_by = :u"), {"u": usuario_raso.id})
    assert firmadas == 1


async def test_teardown_leaves_no_rows_of_the_previous_tests() -> None:
    # Runs after the tests above (file order); anything they left behind is a teardown gap.
    (fila,) = await _filas(
        "SELECT (SELECT count(*) FROM catalogo_bdns WHERE descripcion LIKE :m) AS catalogos, "
        "(SELECT count(*) FROM sync_run WHERE error_detalle LIKE :m) AS runs",
        m=f"{MARCA_TEST}%",
    )
    assert fila == {"catalogos": 0, "runs": 0}


async def test_models_and_migrated_schema_have_no_drift() -> None:
    """Whatever Alembic can compare (columns, types, nullability, indexes, FKs)
    must match between the models and the migrated database. CHECKs are not
    compared by Alembic: `test_check_constraints_*` covers them."""

    def comparar(conexion) -> list:
        with warnings.catch_warnings():
            # Alembic cannot compare the generated `busqueda` column and says so.
            warnings.filterwarnings("ignore", message="Computed default on")
            return compare_metadata(MigrationContext.configure(conexion), Base.metadata)

    async with engine.connect() as conexion:
        diferencias = await conexion.run_sync(comparar)
    assert diferencias == []
