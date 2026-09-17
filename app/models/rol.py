from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import AuditMixin

ROLES_VALIDOS = ("admin", "gestor", "usuario")


class Rol(Base, AuditMixin):
    __tablename__ = "rol"
    __table_args__ = (CheckConstraint(f"codigo IN {ROLES_VALIDOS}", name="ck_rol_codigo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
