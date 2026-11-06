from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin


class AdminLecturaCliente(Base, AuditMixin):
    """Audit log: an admin read data of a client company (who, which company, when = `created_at`).

    Append-only. It stores no payload, only the fact and the kind of read, so it never holds personal data.
    """

    __tablename__ = "admin_lectura_cliente"
    __table_args__ = (Index("ix_admin_lectura_cliente_empresa", "empresa_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id", ondelete="CASCADE"), nullable=False)
    # `ficha` (GET /admin/empresas/{id}) or `usuarios` (GET /admin/usuarios filtered by company).
    accion: Mapped[str] = mapped_column(String(30), nullable=False)
