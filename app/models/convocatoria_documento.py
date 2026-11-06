from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

# Hash, size and fetch time are set together once the file is stored, or all null.
_FETCHED = (
    "(fetched_at IS NULL AND sha256 IS NULL AND tamano IS NULL) OR "
    "(fetched_at IS NOT NULL AND sha256 IS NOT NULL AND tamano IS NOT NULL)"
)


class ConvocatoriaDocumento(Base, AuditMixin):
    """Metadata of a BDNS document cached in our own storage (lazy: created on first request).

    The bytes live in the storage backend under `documentos/<sha256[0:2]>/<sha256>`;
    a row with `fetched_at` NULL means "known but never (successfully) fetched".
    """

    __tablename__ = "convocatoria_documento"
    __table_args__ = (
        UniqueConstraint("convocatoria_id", "bdns_documento_id", name="uq_convocatoria_documento_bdns"),
        CheckConstraint(_FETCHED, name="ck_convocatoria_documento_fetched"),
        CheckConstraint("tamano IS NULL OR tamano >= 0", name="ck_convocatoria_documento_tamano"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    convocatoria_id: Mapped[int] = mapped_column(ForeignKey("convocatoria.id"), nullable=False)
    bdns_documento_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    tamano: Mapped[int | None] = mapped_column(BigInteger)
    mime: Mapped[str | None] = mapped_column(String(150))
    nombre_archivo: Mapped[str | None] = mapped_column(String(300))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
