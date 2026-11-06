"""Contenido del aviso de alerta: asunto, HTML y texto plano.

Funciones puras — reciben la alerta, el usuario y las convocatorias, y
devuelven cadenas. Ni base de datos ni red, para que se puedan probar
afirmando sobre el texto que genera.

Decisiones de producto de esta plantilla:

- **El enlace lleva a la ficha dentro de Subvfy**, no al portal oficial de
  la BDNS. El correo devuelve al usuario al producto, donde puede marcar
  favorito o pedir el análisis con IA. Las rutas son configurables
  (`FRONTEND_*`) porque el routing del Angular puede cambiar.
- **HTML con una alternativa en texto plano**, siempre las dos. Los clientes
  que bloquean HTML muestran la segunda, y tener ambas partes mejora la
  entregabilidad.
- **Se listan como mucho `EMAIL_MAX_CONVOCATORIAS`** y el resto se resume en
  una línea: un digest semanal puede traer decenas y el correo dejaría de
  leerse.
"""

from collections.abc import Sequence
from html import escape

from app.config import settings
from app.models import Alerta, Convocatoria, Usuario

# Paleta mínima, en línea: los clientes de correo ignoran las hojas de
# estilo externas y muchos descartan incluso el <style> del <head>.
_AZUL = "#1c3f94"
_TEXTO = "#1f2328"
_GRIS = "#6b6b70"
_BORDE = "#e1e4e8"


def url_convocatoria(codigo_bdns: str) -> str:
    ruta = settings.frontend_ruta_convocatoria.format(codigo_bdns=codigo_bdns)
    return f"{settings.frontend_base_url.rstrip('/')}{ruta}"


def url_alertas() -> str:
    return f"{settings.frontend_base_url.rstrip('/')}{settings.frontend_ruta_alertas}"


def asunto(alerta: Alerta, cuantas: int) -> str:
    convocatorias = "convocatoria" if cuantas == 1 else "convocatorias"
    nuevas = "nueva" if cuantas == 1 else "nuevas"
    return f"Subvfy · {cuantas} {nuevas} {convocatorias} para «{alerta.nombre}»"


def _descripcion(convocatoria: Convocatoria) -> str:
    """Segunda línea de cada convocatoria: lo que hay, sin huecos vacíos."""
    partes = [
        parte
        for parte in (
            convocatoria.organo_convocante,
            convocatoria.administracion,
            convocatoria.fecha_registro.strftime("%d/%m/%Y") if convocatoria.fecha_registro else None,
            "Financiada por MRR" if convocatoria.financiada_mrr else None,
        )
        if parte
    ]
    return " · ".join(partes)


def cuerpo_texto(alerta: Alerta, usuario: Usuario, convocatorias: Sequence[Convocatoria]) -> str:
    mostradas = convocatorias[: settings.email_max_convocatorias]
    lineas = [
        f"Hola {usuario.nombre},",
        "",
        f"Tu alerta «{alerta.nombre}» ha encontrado {len(convocatorias)} convocatoria(s) nueva(s).",
        "",
    ]
    for convocatoria in mostradas:
        lineas.append(f"* {convocatoria.titulo}")
        descripcion = _descripcion(convocatoria)
        if descripcion:
            lineas.append(f"  {descripcion}")
        lineas.append(f"  {url_convocatoria(convocatoria.codigo_bdns)}")
        lineas.append("")

    restantes = len(convocatorias) - len(mostradas)
    if restantes > 0:
        lineas.append(f"Y {restantes} más. Verlas todas en {url_alertas()}")
        lineas.append("")

    lineas += [
        "---",
        f"Recibes este aviso por tu alerta «{alerta.nombre}» ({alerta.frecuencia}).",
        f"Puedes pausarla o cambiar sus criterios en {url_alertas()}",
    ]
    return "\n".join(lineas)


def cuerpo_html(alerta: Alerta, usuario: Usuario, convocatorias: Sequence[Convocatoria]) -> str:
    mostradas = convocatorias[: settings.email_max_convocatorias]
    restantes = len(convocatorias) - len(mostradas)

    tarjetas = []
    for convocatoria in mostradas:
        descripcion = _descripcion(convocatoria)
        linea_descripcion = (
            f'<div style="color:{_GRIS};font-size:13px;margin-top:4px;">{escape(descripcion)}</div>'
            if descripcion
            else ""
        )
        tarjetas.append(
            f'<tr><td style="padding:14px 0;border-bottom:1px solid {_BORDE};">'
            f'<a href="{escape(url_convocatoria(convocatoria.codigo_bdns))}" '
            f'style="color:{_AZUL};font-size:15px;font-weight:600;text-decoration:none;">'
            f"{escape(convocatoria.titulo)}</a>"
            f"{linea_descripcion}"
            f'<div style="color:{_GRIS};font-size:12px;margin-top:4px;">'
            f"Código BDNS {escape(convocatoria.codigo_bdns)}</div>"
            f"</td></tr>"
        )

    bloque_restantes = (
        f'<p style="font-size:14px;"><a href="{escape(url_alertas())}" style="color:{_AZUL};">'
        f"Y {restantes} convocatoria(s) más — verlas todas en Subvfy</a></p>"
        if restantes > 0
        else ""
    )

    cuantas = len(convocatorias)
    titular = f"{cuantas} {'nueva convocatoria' if cuantas == 1 else 'nuevas convocatorias'}"

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f6f8;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6f8;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:600px;background:#ffffff;border:1px solid {_BORDE};border-radius:8px;
                    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{_TEXTO};">
        <tr><td style="padding:20px 24px;border-bottom:2px solid {_AZUL};">
          <span style="font-size:18px;font-weight:700;color:{_AZUL};">Subvfy</span>
        </td></tr>
        <tr><td style="padding:24px;">
          <p style="margin:0 0 4px;font-size:15px;">Hola {escape(usuario.nombre)},</p>
          <p style="margin:0 0 16px;font-size:15px;">
            Tu alerta <strong>{escape(alerta.nombre)}</strong> ha encontrado {titular}.
          </p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(tarjetas)}</table>
          {bloque_restantes}
        </td></tr>
        <tr><td style="padding:16px 24px;background:#fafafa;border-top:1px solid {_BORDE};
                       color:{_GRIS};font-size:12px;border-radius:0 0 8px 8px;">
          Recibes este aviso por tu alerta «{escape(alerta.nombre)}» ({escape(alerta.frecuencia)}).
          <a href="{escape(url_alertas())}" style="color:{_AZUL};">Pausarla o cambiar sus criterios</a>.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
