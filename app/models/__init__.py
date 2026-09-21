"""Agrega todos los modelos para que Base.metadata los conozca (Alembic
autogenerate los necesita importados, no solo definidos en sus archivos).

Implementan, tabla por tabla, el esquema ya diseñado en schema-subvfy.sql.
"""

from app.models.alerta import Alerta
from app.models.alerta_ejecucion import AlertaEjecucion
from app.models.alerta_ejecucion_convocatoria import AlertaEjecucionConvocatoria
from app.models.alerta_organo import AlertaOrgano
from app.models.alerta_region import AlertaRegion
from app.models.analisis_ia import AnalisisIA
from app.models.conversacion_asistente import ConversacionAsistente
from app.models.convocatoria import Convocatoria
from app.models.empresa import Empresa
from app.models.empresa_palabra_clave import EmpresaPalabraClave
from app.models.favorito import Favorito
from app.models.mensaje_asistente import MensajeAsistente
from app.models.rol import Rol
from app.models.usuario import Usuario

__all__ = [
    "Alerta",
    "AlertaEjecucion",
    "AlertaEjecucionConvocatoria",
    "AlertaOrgano",
    "AlertaRegion",
    "AnalisisIA",
    "Convocatoria",
    "ConversacionAsistente",
    "Empresa",
    "EmpresaPalabraClave",
    "Favorito",
    "MensajeAsistente",
    "Rol",
    "Usuario",
]
