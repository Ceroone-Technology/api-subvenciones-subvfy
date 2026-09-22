"""Mixin de auditoría, compartido por todos los modelos.

Reproduce el patrón uniforme de `schema-subvfy.sql`: created_at/updated_at
automáticos, y created_by/updated_by apuntando a usuario.id. `use_alter=True`
en esas dos FKs reproduce el mismo rodeo que el propio SQL usa (ALTER TABLE
al final): rol/empresa/usuario tienen entre sí una dependencia circular de
auditoría, y esto le permite a Alembic ordenar el CREATE TABLE sin
bloquearse por el ciclo.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class AuditMixin:
    if TYPE_CHECKING:
        # Lo define cada modelo; se declara solo para el type checker, que
        # si no ve un atributo desconocido en los nombres de FK de abajo.
        __tablename__: str
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @declared_attr
    def created_by(cls) -> Mapped[int | None]:
        return mapped_column(
            ForeignKey("usuario.id", use_alter=True, name=f"fk_{cls.__tablename__}_created_by"),
            nullable=True,
        )

    @declared_attr
    def updated_by(cls) -> Mapped[int | None]:
        return mapped_column(
            ForeignKey("usuario.id", use_alter=True, name=f"fk_{cls.__tablename__}_updated_by"),
            nullable=True,
        )
