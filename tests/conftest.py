"""Shared fixtures and helpers for the tabdiff test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


def docker_available() -> bool:
    try:
        import subprocess  # noqa: PLC0415

        r = subprocess.run(
            ["docker", "info", "--format", "ok"],
            capture_output=True,
            timeout=20,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def pg_dsn_from_env() -> str | None:
    return os.environ.get("TABDIFF_TEST_PG_DSN")


requires_pg = pytest.mark.postgres
