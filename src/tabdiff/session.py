"""Shared DuckDB session management.

One in-memory DuckDB connection is the hub: every source is bound into it as
a view or an attached database, so joindiff can always operate in SQL.
"""

from __future__ import annotations

import contextlib
from typing import Any

import duckdb


def quote_ident(name: str) -> str:
    """Quote an SQL identifier for DuckDB."""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """Quote an SQL string literal for DuckDB."""
    return "'" + value.replace("'", "''") + "'"


class Session:
    """A DuckDB connection plus bookkeeping for bound sources."""

    def __init__(self) -> None:
        self.con: Any = duckdb.connect(":memory:")
        self.views: dict[str, str] = {}
        self.attached: dict[str, str] = {}

    # -- execution ---------------------------------------------------------

    def execute(self, sql: str) -> Any:
        return self.con.execute(sql)

    def arrow(self, sql: str) -> Any:
        """Run SQL and fetch the result as a PyArrow Table."""
        result = self.con.execute(sql).arrow()
        # Some duckdb versions hand back a record-batch reader here.
        if hasattr(result, "read_all"):
            return result.read_all()
        return result

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        return list(self.con.execute(sql).fetchall())

    def scalar(self, sql: str) -> Any:
        row = self.con.execute(sql).fetchone()
        if row is None:
            msg = f"query returned no rows: {sql!r}"
            raise RuntimeError(msg)
        return row[0]

    # -- registration ------------------------------------------------------

    def create_view(self, name: str, sql: str) -> None:
        qn = quote_ident(name)
        self.con.execute(f"CREATE OR REPLACE VIEW {qn} AS {sql}")
        self.views[name] = sql

    def attach_duckdb(self, path: str, alias: str) -> None:
        qa, qp = quote_ident(alias), quote_literal(path)
        self.con.execute(f"ATTACH IF NOT EXISTS {qp} AS {qa}")
        self.attached[alias] = path

    def attach_postgres(self, conninfo: str, alias: str) -> None:
        """Install (once) and load the postgres extension, then ATTACH."""
        try:
            self.con.execute("INSTALL postgres")
        except Exception as exc:
            msg = (
                "could not install the DuckDB postgres extension "
                "(needed to reach Postgres sources); network down?"
            )
            raise RuntimeError(msg) from exc
        self.con.execute("LOAD postgres")
        qa, qc = quote_ident(alias), quote_literal(conninfo)
        self.con.execute(f"ATTACH IF NOT EXISTS {qc} AS {qa} (TYPE POSTGRES)")
        self.attached[alias] = conninfo

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.con.close()
