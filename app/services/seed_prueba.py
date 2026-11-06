"""Realistic test data for ONE account, for local demos and manual testing (`python -m app.cli seed-prueba`).

Rules that make it safe:

- It refuses to run unless `ENVIRONMENT` is `development` (the default of `Settings` is `production`).
- It only touches the rows of the given user and of that user's company. It never reads or writes anyone else's
  favorites, alerts, preferences or board cards, and it never changes `actividad_*`, the name or the NIF.
- It is idempotent: each block checks what it already created and does nothing the second time (see each function).
- It uses real grants already in the database; it creates none.
- Everything is one transaction: either all of it is stored or none.
- Favorite notes end with `MARCA`, so the seeded rows can be told apart (and removed) later.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as insert_postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Alerta,
    AlertaEjecucion,
    AlertaEjecucionConvocatoria,
    Convocatoria,
    Descarte,
    Empresa,
    EmpresaInteres,
    Favorito,
    PreferenciaAviso,
    Seguimiento,
    SeguimientoEvento,
    SeguimientoTarea,
    Usuario,
)
from app.services.convocatoria_estado import ABIERTA, estado_expr, hoy_madrid

MARCA = "(dato de prueba)"
PREFIJO_ALERTA = "Prueba: "
TELEFONO = "+34 600 123 456"
OBJETIVO_FAVORITOS = 8
OBJETIVO_DESCARTES = 2
# Days from today at which the seeded favorites should close (varied on purpose: urgent to far).
DIAS_CIERRE = (2, 5, 9, 13, 20, 30, 50, 90)

PERFIL = {
    "tamano": "pequena",
    "empleados": 35,
    "facturacion_tramo": "2m_10m",
    "provincia": "Madrid",
    "municipio": "Madrid",
    "proyecto_previsto": (
        "Digitalizar la gestión comercial y financiera con un ERP integrado, automatizar la facturación y "
        "reforzar la ciberseguridad de la plataforma de cliente durante los próximos 12 meses."
    ),
}
INTERESES = ("erp", "automatizacion", "ciberseguridad", "id_producto")
NOTAS = (
    "Encaja con el proyecto del ERP; revisar si cubre licencias.",
    "Pedir presupuesto a dos proveedores antes de presentar.",
    "Compatible con la ayuda autonómica? Confirmar con la gestoría.",
    "Plazo ajustado: hablar con dirección esta semana.",
    "Posible para el área de I+D; falta la memoria técnica.",
    "Guardada para el próximo trimestre.",
    "Revisar requisitos de tamaño de empresa.",
    "Buena opción de ciberseguridad.",
)
# Board plan: (state, result, days of the task deadlines base). 8 cards over the 4 columns.
TABLERO = (
    ("interesa", None),
    ("interesa", None),
    ("preparando", None),
    ("preparando", None),
    ("preparando", None),
    ("presentada", None),
    ("resuelta", "concedida"),
    ("resuelta", "denegada"),
)
TAREAS = (
    ("documento", "Certificado de estar al corriente con Hacienda", 6),
    ("documento", "Memoria técnica del proyecto", 12),
    ("documento", "Presupuestos de proveedores", 10),
    ("tarea", "Reunión con la gestoría", 3),
    ("tarea", "Firmar la declaración responsable", 15),
)
ALERTAS = (
    # (name, text, level, frequency, channel, runs: [(hours ago, found, state)])
    (
        "Digitalización pyme", "digitalización", None, "diaria", "email",
        [(70, 3, "enviado"), (46, 0, "sin_novedades"), (22, 5, "enviado")],
    ),
    ("I+D empresarial", "innovación", "estado", "semanal", "ambos", [(160, 2, "enviado"), (30, 1, "enviado")]),
    (
        "Ciberseguridad", "ciberseguridad", "ccaa", "diaria", "plataforma",
        [(90, 1, "enviado"), (66, 0, "sin_novedades"), (18, 0, "error")],
    ),
)  # fmt: skip
PREFERENCIAS = (
    ("nuevas", True, True),
    ("plazos", True, True),
    ("alertas", True, True),
    ("semanal", True, True),
    ("seguimiento", False, True),
)


class SeedRechazado(Exception):
    """The seed must not run (production, or the user does not exist)."""


@dataclass
class Resumen:
    lineas: list[str] = field(default_factory=list)

    def anadir(self, texto: str) -> None:
        self.lineas.append(texto)


def exigir_entorno_de_desarrollo() -> None:
    if settings.environment != "development":
        raise SeedRechazado(
            f"seed-prueba solo corre con ENVIRONMENT=development (ahora: {settings.environment}). No se ha tocado nada."
        )


async def _convocatorias_abiertas(db: AsyncSession, hoy: date) -> list[Convocatoria]:
    return list(
        await db.scalars(
            select(Convocatoria)
            .where(
                Convocatoria.removed_at.is_(None),
                estado_expr() == ABIERTA,
                Convocatoria.fecha_fin_solicitud >= hoy,
            )
            .order_by(Convocatoria.fecha_fin_solicitud, Convocatoria.codigo_bdns)
        )
    )


def _mas_cercana(pool: list[Convocatoria], destino: date, excluidas: set[int]) -> Convocatoria | None:
    candidatas = [c for c in pool if c.id not in excluidas and c.fecha_fin_solicitud is not None]
    if not candidatas:
        return None
    return min(candidatas, key=lambda c: (abs(((c.fecha_fin_solicitud or destino) - destino).days), c.codigo_bdns))


async def _perfil_empresa(db: AsyncSession, empresa: Empresa, uid: int, resumen: Resumen) -> None:
    cambios = {k: v for k, v in PERFIL.items() if getattr(empresa, k) != v}
    for campo, valor in cambios.items():
        setattr(empresa, campo, valor)
    if cambios:
        empresa.updated_by = uid
    consulta = select(EmpresaInteres.interes_codigo).where(EmpresaInteres.empresa_id == empresa.id)
    actuales = set(await db.scalars(consulta))
    nuevos = [i for i in INTERESES if i not in actuales]
    for codigo in nuevos:
        db.add(EmpresaInteres(empresa_id=empresa.id, interes_codigo=codigo, created_by=uid, updated_by=uid))
    codigo_actividad = empresa.actividad_codigo
    pendiente = "con actividad pendiente" if codigo_actividad is None else f"actividad {codigo_actividad}"
    resumen.anadir(
        f"Empresa {empresa.razon_social}: {len(cambios)} campo(s) de perfil actualizados, "
        f"{len(nuevos)} interés(es) añadidos ({pendiente}, sin tocar)."
    )


async def _telefono(usuario: Usuario, resumen: Resumen) -> None:
    if usuario.telefono is None:
        usuario.telefono = TELEFONO
        resumen.anadir(f"Teléfono: {TELEFONO} (estaba vacío).")
    else:
        resumen.anadir("Teléfono: ya tenía uno, no se toca.")


async def _favoritos(db: AsyncSession, usuario: Usuario, pool: list[Convocatoria], hoy: date, resumen: Resumen) -> None:
    ya_sembrados = await db.scalar(
        select(func.count()).where(Favorito.usuario_id == usuario.id, Favorito.nota.like(f"%{MARCA}"))
    )
    if ya_sembrados:
        resumen.anadir(f"Favoritos: ya había {ya_sembrados} de prueba, no se añaden más.")
        return
    propios = set(await db.scalars(select(Favorito.convocatoria_id).where(Favorito.usuario_id == usuario.id)))
    elegidas: list[Convocatoria] = []
    for dias in DIAS_CIERRE[:OBJETIVO_FAVORITOS]:
        c = _mas_cercana(pool, hoy + timedelta(days=dias), propios | {e.id for e in elegidas})
        if c is not None:
            elegidas.append(c)
    for c, nota in zip(elegidas, NOTAS, strict=False):
        db.add(
            Favorito(
                usuario_id=usuario.id, convocatoria_id=c.id, nota=f"{nota} {MARCA}",
                created_by=usuario.id, updated_by=usuario.id,
            )  # fmt: skip
        )
    cierres = ", ".join(str(c.fecha_fin_solicitud) for c in elegidas)
    resumen.anadir(f"Favoritos: {len(elegidas)} añadidos con nota, cierran: {cierres}.")


async def _alertas(db: AsyncSession, usuario: Usuario, pool: list[Convocatoria], resumen: Resumen) -> None:
    uid, ahora = usuario.id, datetime.now(UTC)
    creadas = ejecuciones = 0
    for indice, (nombre, texto, nivel, frecuencia, canal, corridas) in enumerate(ALERTAS):
        nombre_completo = f"{PREFIJO_ALERTA}{nombre}"
        existe = await db.scalar(select(Alerta.id).where(Alerta.usuario_id == uid, Alerta.nombre == nombre_completo))
        if existe is not None:
            continue
        alerta = Alerta(
            usuario_id=uid, nombre=nombre_completo, texto_busqueda=texto, nivel_administracion=nivel,
            frecuencia=frecuencia, canal_notificacion=canal, created_by=uid, updated_by=uid,
        )  # fmt: skip
        db.add(alerta)
        await db.flush()
        # Each alert links different grants from the pool (UNIQUE per alert and grant).
        trozo = iter(pool[indice * 5 :])
        for horas, encontradas, estado in corridas:
            ejecucion = AlertaEjecucion(
                alerta_id=alerta.id, fecha_ejecucion_at=ahora - timedelta(hours=horas),
                convocatorias_encontradas=encontradas, estado_envio=estado,
                detalle_error="[ejecución] BdnsNoDisponible: la BDNS no respondió (dato de prueba)"
                if estado == "error" else None,
                created_by=uid, updated_by=uid,
            )  # fmt: skip
            db.add(ejecucion)
            await db.flush()
            for _ in range(encontradas):
                c = next(trozo, None)
                if c is not None:
                    db.add(
                        AlertaEjecucionConvocatoria(
                            alerta_ejecucion_id=ejecucion.id, alerta_id=alerta.id, convocatoria_id=c.id,
                            created_by=uid, updated_by=uid,
                        )  # fmt: skip
                    )
            ejecuciones += 1
        alerta.ultima_ejecucion_at = max(ahora - timedelta(hours=h) for h, _, _ in corridas)
        creadas += 1
    resumen.anadir(f"Alertas: {creadas} creadas con {ejecuciones} ejecuciones ({len(ALERTAS) - creadas} ya existían).")


async def _tablero(
    db: AsyncSession, usuario: Usuario, pool: list[Convocatoria], hoy: date, resumen: Resumen
) -> None:
    hay = await db.scalar(select(func.count()).where(Seguimiento.empresa_id == usuario.empresa_id))
    if hay:
        resumen.anadir(f"Seguimiento: el tablero ya tiene {hay} tarjeta(s), no se añaden más.")
        return
    uid = usuario.id
    personas = list(
        await db.scalars(
            select(Usuario.id)
            .where(Usuario.empresa_id == usuario.empresa_id, Usuario.estado == "activo")
            .order_by(Usuario.id)
        )
    )
    if uid not in personas:
        personas.insert(0, uid)
    usadas: set[int] = set()
    columnas: dict[str, int] = {}
    # A spread of budgets and deadlines: the pool is sorted by closing date; take every few-th grant.
    paso = max(1, len(pool) // len(TABLERO))
    for i, (estado, resultado) in enumerate(TABLERO):
        c = next((x for x in pool[i * paso :] if x.id not in usadas), None) or next(
            (x for x in pool if x.id not in usadas), None
        )
        if c is None:
            break
        usadas.add(c.id)
        responsable = personas[i % len(personas)] if i != 0 else uid
        seg = Seguimiento(
            empresa_id=usuario.empresa_id, convocatoria_id=c.id, estado=estado, resultado=resultado,
            responsable_usuario_id=responsable,
            fecha_presentacion=hoy - timedelta(days=20 - i) if estado in ("presentada", "resuelta") else None,
            created_by=uid, updated_by=uid,
        )  # fmt: skip
        db.add(seg)
        await db.flush()
        eventos: list[tuple[str, str | None, str]] = [("creada", None, "interesa")]
        if estado != "interesa":
            eventos.append(("movida", "interesa", estado))
        for tipo_evento, anterior, nuevo in eventos:
            db.add(
                SeguimientoEvento(
                    seguimiento_id=seg.id, usuario_id=uid, tipo=tipo_evento, estado_anterior=anterior,
                    estado_nuevo=nuevo, created_by=uid, updated_by=uid,
                )  # fmt: skip
            )
        if estado != "interesa":
            for j, (tipo, titulo, dias) in enumerate(TAREAS[: 2 + (i % 4)]):
                hecha = estado in ("presentada", "resuelta") or j == 0
                db.add(
                    SeguimientoTarea(
                        seguimiento_id=seg.id, titulo=titulo, tipo=tipo, hecha=hecha,
                        fecha_limite=hoy + timedelta(days=dias + i) if not hecha or estado == "preparando" else None,
                        asignado_a=responsable, created_by=uid, updated_by=uid,
                    )  # fmt: skip
                )
        columnas[estado] = columnas.get(estado, 0) + 1
    detalle = ", ".join(f"{e}: {n}" for e, n in columnas.items())
    resumen.anadir(f"Seguimiento: {sum(columnas.values())} tarjetas ({detalle}), con tareas y responsable.")


async def _descartes(db: AsyncSession, usuario: Usuario, pool: list[Convocatoria], resumen: Resumen) -> None:
    hay = await db.scalar(select(func.count()).where(Descarte.usuario_id == usuario.id))
    if hay:
        resumen.anadir(f"Descartes: ya había {hay}, no se añaden más.")
        return
    propios = set(await db.scalars(select(Favorito.convocatoria_id).where(Favorito.usuario_id == usuario.id)))
    elegidas = [c for c in reversed(pool) if c.id not in propios][:OBJETIVO_DESCARTES]
    for c in elegidas:
        db.add(Descarte(usuario_id=usuario.id, convocatoria_id=c.id, created_by=usuario.id, updated_by=usuario.id))
    resumen.anadir(f"Descartes: {len(elegidas)} convocatorias ocultas del radar (las de cierre más lejano).")


async def _preferencias(db: AsyncSession, usuario: Usuario, resumen: Resumen) -> None:
    for tipo, email, app in PREFERENCIAS:
        valores = {"email": email, "app": app}
        await db.execute(
            insert_postgresql(PreferenciaAviso)
            .values(usuario_id=usuario.id, tipo=tipo, created_by=usuario.id, updated_by=usuario.id, **valores)
            .on_conflict_do_update(
                index_elements=[PreferenciaAviso.usuario_id, PreferenciaAviso.tipo], set_={**valores}
            )
        )
    resumen.anadir(f"Preferencias de aviso: {len(PREFERENCIAS)} tipos guardados.")


async def sembrar(db: AsyncSession, email: str, *, hoy: date | None = None) -> Resumen:
    """Seeds the account of `email`. Commits once at the end; raises `SeedRechazado` before touching anything."""
    exigir_entorno_de_desarrollo()
    usuario = await db.scalar(select(Usuario).where(Usuario.email == email))
    if usuario is None:
        raise SeedRechazado(f"No existe ningún usuario con el email {email}. No se ha tocado nada.")
    empresa = await db.get(Empresa, usuario.empresa_id)
    assert empresa is not None
    hoy = hoy or await db.scalar(select(hoy_madrid())) or date.today()
    pool = await _convocatorias_abiertas(db, hoy)

    resumen = Resumen()
    resumen.anadir(
        f"Cuenta {usuario.email} (usuario {usuario.id}, empresa {empresa.id}); "
        f"{len(pool)} convocatorias abiertas disponibles."
    )
    await _perfil_empresa(db, empresa, usuario.id, resumen)
    await _telefono(usuario, resumen)
    await _favoritos(db, usuario, pool, hoy, resumen)
    await _alertas(db, usuario, pool, resumen)
    await _tablero(db, usuario, pool, hoy, resumen)
    await _descartes(db, usuario, pool, resumen)
    await _preferencias(db, usuario, resumen)
    await db.commit()
    return resumen
