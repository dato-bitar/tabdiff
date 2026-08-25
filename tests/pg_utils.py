"""Postgres test helpers: container startup and fast loading via DuckDB."""

from __future__ import annotations

from typing import Any


def _normalize_dsn(url: str) -> str:
    """testcontainers may emit 'postgresql+psycopg2://'; libpq wants plain."""
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


def docker_reachable() -> bool:
    import subprocess

    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "ok"],
            capture_output=True,
            timeout=20,
            check=False,
        )
        return probe.returncode == 0
    except Exception:
        return False


def load_arrow_into_pg(session: Any, dsn: str, table: str, arrow_table: Any) -> None:
    """Create public.table in the postgres at dsn, filled with arrow_table."""
    import contextlib

    session.attach_postgres(dsn, "pgload")
    session.con.register("tabdiff_load_src", arrow_table)
    qtable = '"' + table.replace('"', '""') + '"'
    with contextlib.suppress(Exception):
        session.execute(f"DROP TABLE IF EXISTS pgload.public.{qtable}")
    session.execute(
        f"CREATE TABLE pgload.public.{qtable} AS SELECT * FROM tabdiff_load_src"
    )
    session.con.unregister("tabdiff_load_src")


def pg_source_spec(dsn: str, table: str) -> str:
    """Build a tabdiff source URL for a table in the public schema."""
    base = dsn.rstrip("/")
    return f"{base}/public/{table}"


def load_arrow_into_pg(session: Any, dsn: str, table: str, arrow_table: Any) -> None:
    """Create schema.table in the postgres at dsn, filled with arrow_table."""
    session.attach_postgres(dsn, "pgload")
    session.con.register("tabdiff_load_src", arrow_table)
    qtable = '"' + table.replace('"', '""') + '"'
    try:
        session.execute(f"DROP TABLE IF EXISTS pgload.public.{qtable}")
    except Exception:
        pass
    session.execute(f"CREATE TABLE pgload.public.{qtable} AS SELECT * FROM tabdiff_load_src")
    session.con.unregister("tabdiff_load_src")


def pg_source_spec(dsn: str, table: str) -> str:
    """Build a tabdiff source URL for a table in the public schema."""
    base = dsn.rstrip("/")
    return f"{base}/public/{table}"
