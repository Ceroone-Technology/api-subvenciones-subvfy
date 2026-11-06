"""Schema of the document cache (PR10, task 10.1). Inspects the real database catalogs."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.models import ConvocatoriaDocumento, DocumentoAcceso
from app.models.documento_acceso import ORIGENES_ACCESO
from tests.conftest import Sesion, crear_convocatoria_en_bd

SHA = "a" * 64


async def _filas(sql: str, **params) -> list[dict]:
    async with AsyncSessionLocal() as db:
        return [dict(f) for f in (await db.execute(text(sql), params)).mappings().all()]


async def test_origenes_de_acceso() -> None:
    assert ORIGENES_ACCESO == ("bdns", "cache")


@pytest.mark.parametrize("tabla", ["convocatoria_documento", "documento_acceso"])
async def test_audit_fks_present(tabla: str) -> None:
    filas = await _filas(
        "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'f'", t=tabla
    )
    nombres = {f["conname"] for f in filas}
    assert {f"fk_{tabla}_created_by", f"fk_{tabla}_updated_by"} <= nombres


async def test_constraints_and_indexes_exist() -> None:
    cons = {f["conname"] for f in await _filas("SELECT conname FROM pg_constraint")}
    assert {
        "uq_convocatoria_documento_bdns",
        "ck_convocatoria_documento_fetched",
        "ck_documento_acceso_origen",
    } <= cons
    idx = {f["indexname"] for f in await _filas("SELECT indexname FROM pg_indexes")}
    assert {"ix_documento_acceso_documento", "ix_documento_acceso_usuario"} <= idx


async def test_unique_per_convocatoria_and_bdns_id() -> None:
    conv = await crear_convocatoria_en_bd("Docs unique")
    async with AsyncSessionLocal() as db:
        db.add(ConvocatoriaDocumento(convocatoria_id=conv.id, bdns_documento_id=1))
        await db.commit()
    async with AsyncSessionLocal() as db:
        db.add(ConvocatoriaDocumento(convocatoria_id=conv.id, bdns_documento_id=1))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.parametrize(
    "extra",
    [{"sha256": SHA}, {"fetched_at": datetime.now(UTC)}, {"sha256": SHA, "tamano": 5}, {"tamano": 5}],
)
async def test_fetched_check_is_all_or_nothing(extra: dict) -> None:
    conv = await crear_convocatoria_en_bd("Docs check")
    async with AsyncSessionLocal() as db:
        db.add(ConvocatoriaDocumento(convocatoria_id=conv.id, bdns_documento_id=2, **extra))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_fetched_row_and_access_event_roundtrip(usuario_raso: Sesion) -> None:
    conv = await crear_convocatoria_en_bd("Docs ok")
    async with AsyncSessionLocal() as db:
        doc = ConvocatoriaDocumento(
            convocatoria_id=conv.id, bdns_documento_id=3, sha256=SHA, tamano=10, mime="application/pdf",
            fetched_at=datetime.now(UTC),
        )  # fmt: skip
        db.add(doc)
        await db.flush()
        db.add(DocumentoAcceso(documento_id=doc.id, usuario_id=usuario_raso.id, origen="bdns"))
        await db.commit()
    async with AsyncSessionLocal() as db:
        origenes = (
            (await db.execute(select(DocumentoAcceso.origen).where(DocumentoAcceso.usuario_id == usuario_raso.id)))
            .scalars()
            .all()
        )
        assert origenes == ["bdns"]


async def test_origen_check_rejects_unknown(usuario_raso: Sesion) -> None:
    conv = await crear_convocatoria_en_bd("Docs origen")
    async with AsyncSessionLocal() as db:
        doc = ConvocatoriaDocumento(convocatoria_id=conv.id, bdns_documento_id=4)
        db.add(doc)
        await db.flush()
        db.add(DocumentoAcceso(documento_id=doc.id, usuario_id=usuario_raso.id, origen="otro"))
        with pytest.raises(IntegrityError):
            await db.commit()
