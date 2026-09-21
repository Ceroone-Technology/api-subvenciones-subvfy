from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

NIVELES_ADMINISTRACION = ("estado", "ccaa", "local", "otros")


class Convocatoria(Base, AuditMixin):
    """Caché local de convocatorias de la BDNS. La fuente de verdad sigue
    siendo la API de la BDNS; esta tabla solo evita perder el histórico si
    la API externa cambia, y es lo que referencian favoritos/alertas."""

    __tablename__ = "convocatoria"
    __table_args__ = (
        CheckConstraint(f"nivel_administracion IN {NIVELES_ADMINISTRACION}", name="ck_convocatoria_nivel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # unique=True ya genera el índice; el ix_convocatoria_codigo_bdns
    # separado de schema-subvfy.sql era redundante sobre la misma columna.
    codigo_bdns: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    titulo: Mapped[str] = mapped_column(String(500), nullable=False)
    nivel_administracion: Mapped[str | None] = mapped_column(String(20))
    administracion: Mapped[str | None] = mapped_column(String(200))
    organo_convocante: Mapped[str | None] = mapped_column(String(300))
    fecha_registro: Mapped[date | None] = mapped_column(Date)
    url_portal_oficial: Mapped[str | None] = mapped_column(String(500))
    financiada_mrr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    sincronizado_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
