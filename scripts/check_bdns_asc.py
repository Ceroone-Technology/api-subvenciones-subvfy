"""Comprobación en vivo de que la BDNS respeta `direccion=asc` (tarea 2.1).

**No forma parte del CI ni de la suite**: vive fuera de `tests/` y no se importa
desde ningún sitio. Hace UNA sola petición real (sin reintentos) y solo se
ejecuta si `BDNS_LIVE_CHECK=1`, porque la BDNS bloquea rápido (límite de 3
peticiones por segundo, y ya devolvió una página HTML de bloqueo con un 200).

    docker compose cp scripts api:/app/scripts
    docker compose exec -e BDNS_LIVE_CHECK=1 api python -m scripts.check_bdns_asc

Pide la primera página de los últimos 10 días en orden ascendente y decide con
`inferir_direccion`:

- `asc`: la BDNS respeta el orden. Se mantiene D8 (recorrido ascendente). Con
  todas las fechas de la página iguales, concluye `asc` si hay más páginas y la
  fecha es el inicio de la ventana: la página 0 empieza en el día más antiguo.
- `desc`: la BDNS lo ignora. **Plan B (D8 enmendada)**: recorrer en descendente,
  que es lo que hace `ClienteBdns` hoy, con un checkpoint por página dentro de
  la ventana cerrada `[desde, hasta]`. Las inserciones nuevas desplazan las
  páginas, así que el motor debe reanudar por la última `fechaRecepcion` vista
  y no solo por el número de página.
- `indeterminado`: no concluye. Repetir con otra ventana.

Código de salida: 0 asc, 1 desc, 2 no concluyente, 3 error de la BDNS o sin permiso,
4 excepción inesperada (nunca se confunde con el veredicto `desc`).
"""

import asyncio
import os
import sys
from datetime import date, timedelta

from app.services.bdns_cliente import BdnsNoDisponible
from app.services.bdns_sync_cliente import CircuitoAbierto, ClienteBdnsSync, inferir_direccion, orden_de_fechas

VENTANA_DIAS = 10
TAMANO = 20
SALIDA_ERROR_INESPERADO = 4


async def comprobar() -> int:
    hasta = date.today()
    desde = hasta - timedelta(days=VENTANA_DIAS)
    async with ClienteBdnsSync(intentos=1) as cliente:
        try:
            pagina = await cliente.listar_pagina(desde, hasta, pagina=0, tamano=TAMANO, direccion="asc")
        except (BdnsNoDisponible, CircuitoAbierto) as exc:
            print(f"ERROR: {exc}")
            return 3
        peticiones = cliente.peticiones

    fechas = [c.fecha_registro for c in pagina.convocatorias if c.fecha_registro is not None]
    direccion = inferir_direccion(fechas, desde, hasta, hay_mas_paginas=not pagina.ultima)
    print(f"ventana={desde}..{hasta} peticiones={peticiones} filas={len(fechas)} total={pagina.total}")
    if fechas:
        print(f"primera={fechas[0]} ultima={fechas[-1]} orden_en_pagina={orden_de_fechas(fechas)}")
    if direccion == "asc":
        print("RESULTADO: la BDNS respeta direccion=asc (D8 se mantiene).")
        return 0
    if direccion == "desc":
        print("RESULTADO: la BDNS ignora direccion=asc. Aplicar el plan B (recorrido descendente con checkpoint).")
        return 1
    print("RESULTADO: no concluyente. Repetir con otra ventana.")
    return 2


def main() -> int:
    if os.environ.get("BDNS_LIVE_CHECK") != "1":
        print("Desactivado: define BDNS_LIVE_CHECK=1 para hacer UNA petición real a la BDNS.")
        return 3
    try:
        return asyncio.run(comprobar())
    except Exception as exc:
        print(f"ERROR inesperado: {exc!r}")
        return SALIDA_ERROR_INESPERADO


if __name__ == "__main__":
    sys.exit(main())
