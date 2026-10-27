"""Opt-in performance check (PR4, task 4.6): `RUN_PERF=1 pytest -q tests/test_convocatorias_perf.py`.

Seeds 100k `TST` rows (the teardown removes them) and measures the authenticated search.
"""

import os
import random
import statistics
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.database import AsyncSessionLocal

pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(os.getenv("RUN_PERF") != "1", reason="opt-in: set RUN_PERF=1"),
]

FILAS = 100_000
LIMITE_P95_MS = 300

SEMBRAR = text(
    """
    INSERT INTO convocatoria (codigo_bdns, titulo, nivel_administracion, organo_convocante, fecha_registro,
                              fecha_inicio_solicitud, fecha_fin_solicitud, presupuesto_total, organo_ids,
                              region_ids, beneficiarios, financiada_mrr, sincronizado_at)
    SELECT 'TSTPERF' || g,
           'Ayudas ' || (ARRAY['innovación','digitalización','agricultura','turismo','empleo'])[1 + g % 5]
               || ' para ' || (ARRAY['pymes','autónomos','municipios','cooperativas'])[1 + g % 4] || ' ' || g,
           (ARRAY['estado','ccaa','local','otros'])[1 + g % 4],
           'Órgano ' || g % 300,
           date '2024-01-01' + (g % 1000),
           date '2024-01-01' + (g % 1000),
           date '2024-02-01' + (g % 1100),
           (g % 5000) * 100,
           ARRAY[g % 300, g % 40],
           ARRAY[g % 60],
           ARRAY[(ARRAY['PYME','Autónomos','Gran empresa'])[1 + g % 3]],
           g % 7 = 0,
           now()
    FROM generate_series(1, :n) AS g
    """
)

CONSULTAS = [
    {},
    {"q": "innovacion pymes"},
    {"q": "digitalizacion", "orden": "relevancia"},
    {"estado": "Abierta", "orden": "fondos"},
    {"nivel": "ccaa", "organo": 17, "page": 3},
    {"q": "turismo", "estado": ["Abierta", "Pendiente"], "presupuesto_min": 1000},
]


async def test_p95_de_la_busqueda_autenticada_bajo_300_ms(client_usuario: AsyncClient) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(SEMBRAR, {"n": FILAS})
        await db.execute(text("ANALYZE convocatoria"))
        await db.commit()
    for params in CONSULTAS:  # calentamiento: caché de Postgres y primer plan
        await client_usuario.get("/convocatorias", params=params)
    tiempos: list[float] = []
    por_consulta: dict[str, list[float]] = {}
    for _ in range(10):
        for params in random.sample(CONSULTAS, len(CONSULTAS)):
            inicio = time.perf_counter()
            respuesta = await client_usuario.get("/convocatorias", params=params)
            tiempos.append((time.perf_counter() - inicio) * 1000)
            por_consulta.setdefault(str(params), []).append(tiempos[-1])
            assert respuesta.status_code == 200
    p95 = statistics.quantiles(tiempos, n=20)[-1]
    print(f"\nbúsqueda: n={len(tiempos)} mediana={statistics.median(tiempos):.0f} ms p95={p95:.0f} ms")
    for clave, valores in por_consulta.items():
        print(f"  {statistics.median(valores):6.0f} ms  max {max(valores):6.0f}  {clave}")
    assert p95 < LIMITE_P95_MS
