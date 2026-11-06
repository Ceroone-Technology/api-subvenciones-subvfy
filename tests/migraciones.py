"""Helpers to test a migration up and down on a throwaway database (never on the shared one)."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

RAIZ = Path(__file__).resolve().parents[1]


class BaseTemporal:
    def __init__(self, url: str, motor: AsyncEngine) -> None:
        self.url, self.motor = url, motor

    def alembic(self, *args: str) -> None:
        entorno = {**os.environ, "DATABASE_URL": self.url}
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=RAIZ, env=entorno, capture_output=True, text=True, timeout=240,
        )  # fmt: skip
        assert r.returncode == 0, r.stderr[-1500:]


@asynccontextmanager
async def base_temporal() -> AsyncIterator[BaseTemporal]:
    """Creates an empty database, yields it, and always drops it."""
    base = make_url(settings.database_url)
    nombre = f"subvfy_mig_{uuid4().hex[:8]}"
    admin = create_async_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    url = base.set(database=nombre).render_as_string(hide_password=False)
    async with admin.connect() as c:
        await c.execute(text(f'CREATE DATABASE "{nombre}"'))
    scratch = create_async_engine(url, poolclass=NullPool)
    try:
        yield BaseTemporal(url, scratch)
    finally:
        await scratch.dispose()
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)'))
        await admin.dispose()
