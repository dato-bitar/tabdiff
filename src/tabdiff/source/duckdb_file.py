"""DuckDB database-file source (attached)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tabdiff.errors import SourceError
from tabdiff.source.base import BoundSource, RelationSource, describe_to_columns

if TYPE_CHECKING:
    from tabdiff.session import Session


class DuckDBFileSource(RelationSource):
    def __init__(self, session: Session, alias: str, path: Path, table: str) -> None:
        if not path.is_file():
            msg = f"duckdb file not found: {path}"
            raise SourceError(msg)
        session.attach_duckdb(str(path), alias)
        qualified = f'"{alias}"."{table.replace('"', '""')}"'
        # Fail fast on a missing table so errors surface at bind time.
        try:
            cols = describe_to_columns(session.rows(f"DESCRIBE {qualified}"))
        except Exception as exc:
            msg = f"table {table!r} not found in duckdb file {path}: {exc}"
            raise SourceError(msg) from exc
        super().__init__(session, alias, qualified, declared=cols)
        self.table = table


__all__ = ["BoundSource", "DuckDBFileSource"]
