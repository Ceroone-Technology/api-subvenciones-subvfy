"""Normalización de los filtros multivalor de las alertas.

Órganos y regiones se guardan como ids del catálogo de la BDNS
(`alerta_organo.organo_bdns_id`, `alerta_region.region_bdns_id`), que el
frontend ya tiene porque consulta la BDNS directamente. No hay texto libre
que normalizar ni catálogo propio que mantener: basta con dejar la lista en
su forma canónica antes de persistirla.

Vive aparte de `app.services.alertas` para que los schemas puedan usarla sin
un import circular (el servicio importa los schemas).
"""

from collections.abc import Iterable


def normalizar_ids_bdns(ids: Iterable[int]) -> list[int]:
    """Quita duplicados conservando el orden en que llegaron.

    Sin esto, `[9, 9]` chocaría contra `UNIQUE(alerta_id, region_bdns_id)`
    y el cliente recibiría un 500 por algo que no es un error suyo.
    """
    return list(dict.fromkeys(ids))
