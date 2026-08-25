"""Shared fixtures and helpers for the tabdiff test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """A live postgres DSN: env override first, else a testcontainer."""
    env = os.environ.get("TABDIFF_TEST_PG_DSN")
    if env:
        yield env
        return
    from tests.pg_utils import _normalize_dsn, docker_reachable

    if not docker_reachable():
        pytest.skip("docker unavailable")
        return
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")
        return
    with PostgresContainer("postgres:16-alpine") as pg:
        yield _normalize_dsn(pg.get_connection_url())


requires_pg = pytest.mark.postgres
