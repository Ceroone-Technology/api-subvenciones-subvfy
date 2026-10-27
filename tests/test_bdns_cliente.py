"""Cliente HTTP de la BDNS (Hito 4, tarea 2).

**No se llama a la BDNS real**: se inyecta un `httpx.MockTransport`, que es
donde sí es legítimo simular, porque el servicio externo no es nuestro. La base
de datos sigue siendo real en el resto de la suite; aquí no hace falta ninguna.
"""

from typing import Any

import httpx
import pytest

from app.config import settings
from app.services.bdns_cliente import BdnsNoDisponible, ClienteBdns
from app.services.bdns_consulta import ConsultaBdns

CONSULTA = ConsultaBdns(descripcion="digitalización", organos=(1500, 42), regiones=(6,))


def _convocatoria(id_bdns: int, *, mrr: bool = False) -> dict[str, Any]:
    return {
        "id": id_bdns,
        "mrr": mrr,
        "numeroConvocatoria": f"TST{id_bdns}",
        "descripcion": f"Convocatoria {id_bdns}",
        "fechaRecepcion": "2026-09-20",
        "nivel1": "ESTADO",
        "nivel2": "MINISTERIO DE INDUSTRIA",
        "nivel3": None,
    }


def _pagina(contenido: list[dict[str, Any]], *, numero: int, total_paginas: int) -> dict[str, Any]:
    return {
        "content": contenido,
        "number": numero,
        "size": len(contenido),
        "totalPages": total_paginas,
        "totalElements": total_paginas * max(len(contenido), 1),
        "last": numero >= total_paginas - 1,
    }


def _cliente(manejador) -> ClienteBdns:
    return ClienteBdns(
        httpx.AsyncClient(transport=httpx.MockTransport(manejador), base_url="https://bdns.example/api")
    )


# --- Una página ---------------------------------------------------------


async def test_una_pagina_se_parsea_a_dataclasses() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pagina([_convocatoria(7, mrr=True)], numero=0, total_paginas=1))

    async with _cliente(manejador) as cliente:
        pagina = await cliente.buscar_pagina(CONSULTA)

    assert pagina.total_paginas == 1
    assert pagina.ultima is True
    convocatoria = pagina.convocatorias[0]
    assert (convocatoria.id_bdns, convocatoria.codigo_bdns) == (7, "TST7")
    assert convocatoria.titulo == "Convocatoria 7"
    assert convocatoria.fecha_registro.isoformat() == "2026-09-20"
    assert convocatoria.financiada_mrr is True


async def test_la_peticion_lleva_los_parametros_de_la_consulta() -> None:
    """Incluidos los repetidos de órganos y regiones, y la paginación."""
    vistas: list[httpx.URL] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion.url)
        return httpx.Response(200, json=_pagina([], numero=0, total_paginas=1))

    async with _cliente(manejador) as cliente:
        await cliente.buscar_pagina(CONSULTA, pagina=3)

    url = vistas[0]
    assert url.path.endswith("/convocatorias/busqueda")
    assert url.params["descripcion"] == "digitalización"
    assert url.params.get_list("organos") == ["1500", "42"]
    assert url.params.get_list("regiones") == ["6"]
    assert url.params["page"] == "3"
    assert url.params["pageSize"] == str(settings.bdns_tamano_pagina)
    # Lo más reciente primero: si se corta por el tope, se corta por lo viejo.
    assert (url.params["order"], url.params["direccion"]) == ("fechaRecepcion", "desc")


async def test_una_respuesta_sin_content_no_revienta() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": None, "last": True})

    async with _cliente(manejador) as cliente:
        pagina = await cliente.buscar_pagina(CONSULTA)
    assert pagina.convocatorias == []


# --- Recorrido de páginas ----------------------------------------------


async def test_recorre_todas_las_paginas_hasta_la_ultima() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        numero = int(peticion.url.params["page"])
        return httpx.Response(
            200, json=_pagina([_convocatoria(numero * 10 + i) for i in range(2)], numero=numero, total_paginas=3)
        )

    async with _cliente(manejador) as cliente:
        convocatorias = await cliente.buscar_todo(CONSULTA)

    assert [c.id_bdns for c in convocatorias] == [0, 1, 10, 11, 20, 21]


async def test_el_tope_de_paginas_corta_el_recorrido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una consulta demasiado abierta no puede llevarse el ciclo por delante."""
    monkeypatch.setattr(settings, "bdns_max_paginas", 2)
    peticiones = 0

    def manejador(peticion: httpx.Request) -> httpx.Response:
        nonlocal peticiones
        peticiones += 1
        numero = int(peticion.url.params["page"])
        # 100 páginas: nunca llega la última.
        return httpx.Response(200, json=_pagina([_convocatoria(numero)], numero=numero, total_paginas=100))

    async with _cliente(manejador) as cliente:
        convocatorias = await cliente.buscar_todo(CONSULTA)

    assert peticiones == 2
    assert len(convocatorias) == 2


async def test_solo_mrr_se_filtra_en_el_cliente() -> None:
    """La BDNS no tiene parámetro para esto: se usa el campo mrr de cada fila."""
    def manejador(peticion: httpx.Request) -> httpx.Response:
        contenido = [_convocatoria(1, mrr=True), _convocatoria(2), _convocatoria(3, mrr=True)]
        return httpx.Response(200, json=_pagina(contenido, numero=0, total_paginas=1))

    async with _cliente(manejador) as cliente:
        todas = await cliente.buscar_todo(ConsultaBdns(descripcion="x"))
        solo_mrr = await cliente.buscar_todo(ConsultaBdns(descripcion="x", solo_mrr=True))

    assert [c.id_bdns for c in todas] == [1, 2, 3]
    assert [c.id_bdns for c in solo_mrr] == [1, 3]


# --- Errores ------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 429, 500, 503])
async def test_un_error_http_es_bdns_no_disponible(status: int) -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="vaya")

    async with _cliente(manejador) as cliente:
        with pytest.raises(BdnsNoDisponible) as error:
            await cliente.buscar_pagina(CONSULTA)
    assert error.value.status == status


async def test_un_timeout_es_bdns_no_disponible() -> None:
    """Sin reintentos: el aislamiento del ciclo ya registra y sigue."""
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("tardó demasiado", request=peticion)

    async with _cliente(manejador) as cliente:
        with pytest.raises(BdnsNoDisponible) as error:
            await cliente.buscar_pagina(CONSULTA)
    assert error.value.status is None


async def test_un_error_de_red_es_bdns_no_disponible() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta al host", request=peticion)

    async with _cliente(manejador) as cliente:
        with pytest.raises(BdnsNoDisponible):
            await cliente.buscar_pagina(CONSULTA)


async def test_una_respuesta_que_no_es_json_es_bdns_no_disponible() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>mantenimiento</html>")

    async with _cliente(manejador) as cliente:
        with pytest.raises(BdnsNoDisponible):
            await cliente.buscar_pagina(CONSULTA)
