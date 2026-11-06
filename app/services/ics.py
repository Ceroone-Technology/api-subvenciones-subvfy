"""Minimal iCalendar (RFC 5545) writer for all-day events. Pure: no database, no clock unless injected.

Written by hand to avoid a dependency. The two things that matter: text values are escaped (a title with a line
break must never be able to open a new property or a new event) and lines are folded at 75 **octets**.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

CATEGORIAS = {"cierre": "Cierre de plazo", "apertura": "Apertura de plazo", "tarea": "Tarea de seguimiento"}
MAX_OCTETOS = 75


@dataclass(frozen=True)
class EventoIcs:
    uid: str
    fecha: date
    titulo: str
    tipo: str
    descripcion: str | None = None
    url: str | None = None


def escapar(texto: str) -> str:
    """Escapes a TEXT value. CR, LF and CRLF all become `\\n`, so no input can inject a content line."""
    return (
        texto.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def plegar(linea: str) -> str:
    """Folds a content line at 75 octets (never splitting a UTF-8 character); continuations start with a space."""
    datos = linea.encode("utf-8")
    if len(datos) <= MAX_OCTETOS:
        return linea
    trozos: list[str] = []
    actual = ""
    limite = MAX_OCTETOS
    for caracter in linea:
        if len((actual + caracter).encode("utf-8")) > limite:
            trozos.append(actual)
            actual, limite = caracter, MAX_OCTETOS - 1  # the leading space of a continuation counts
        else:
            actual += caracter
    trozos.append(actual)
    return "\r\n ".join(trozos)


def _dia(valor: date) -> str:
    return valor.strftime("%Y%m%d")


def construir_ics(
    eventos: Iterable[EventoIcs], *, nombre: str = "Subvfy", ahora: datetime | None = None
) -> str:
    sello = (ahora or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lineas = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Subvfy//Calendario//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escapar(nombre)}",
    ]
    for e in eventos:
        lineas += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}@subvfy",
            f"DTSTAMP:{sello}",
            f"DTSTART;VALUE=DATE:{_dia(e.fecha)}",
            f"DTEND;VALUE=DATE:{_dia(e.fecha + timedelta(days=1))}",
            f"SUMMARY:{escapar(e.titulo)}",
            f"CATEGORIES:{escapar(CATEGORIAS.get(e.tipo, e.tipo))}",
            "TRANSP:TRANSPARENT",
        ]
        if e.descripcion:
            lineas.append(f"DESCRIPTION:{escapar(e.descripcion)}")
        if e.url:
            lineas.append(f"URL:{escapar(e.url)}")
        lineas.append("END:VEVENT")
    lineas.append("END:VCALENDAR")
    return "".join(plegar(linea) + "\r\n" for linea in lineas)
