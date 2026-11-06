"""Loader for the recorded BDNS responses in `tests/fixtures/bdns/`.

The JSON files are real responses captured from the BDNS (via the frontend
fixtures); tests that need the BDNS use them with `httpx.MockTransport` so the
suite never reaches the real service.
"""

import json
from pathlib import Path
from typing import Any

DIRECTORIO = Path(__file__).parent / "fixtures" / "bdns"


def cargar_fixture(nombre: str) -> Any:
    return json.loads((DIRECTORIO / nombre).read_text(encoding="utf-8"))
