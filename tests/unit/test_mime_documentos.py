"""`mime_seguro`: the BDNS `Content-Type` is not trusted when serving a cached document."""

import pytest

from app.services.documentos import mime_seguro


@pytest.mark.parametrize("mime", ["application/pdf", "image/png", "application/vnd.ms-excel", "Application/PDF"])
def test_mime_normales_pasan(mime: str) -> None:
    assert mime_seguro(mime) == mime.lower()


@pytest.mark.parametrize(
    "mime",
    [None, "", "pdf", "text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/javascript",
     "application/pdf\r\nSet-Cookie: a=b", "a/b c", "x" * 200 + "/pdf"],
)  # fmt: skip
def test_mime_raros_o_activos_caen_a_octet_stream(mime: str | None) -> None:
    assert mime_seguro(mime) == "application/octet-stream"
