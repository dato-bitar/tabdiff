"""Source spec parsing and binding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tabdiff.errors import SourceError
from tabdiff.source.base import BoundSource
from tabdiff.source.duckdb_file import DuckDBFileSource
from tabdiff.source.files import bind_file
from tabdiff.source.postgres import split_postgres_url


@dataclass(frozen=True)
class SourceOptions:
    all_varchar: bool = False


def parse_source_spec(spec: str) -> tuple[str, Path | None, str | None, str | None]:
    """Parse a source spec into (kind, path, url_or_none, table).

    kind is one of ``parquet``, ``csv``, ``duckdb``, ``postgres``.
    For duckdb specs, ``path`` is the database file and ``table`` the table
    name. For postgres, ``url`` carries the full connection URL and ``table``
    the (possibly schema-qualified) table name.
    """
    spec = spec.strip()
    if spec.startswith(("postgres://", "postgresql://")):
        from urllib.parse import urlsplit  # noqa: PLC0415

        _conninfo, _dbname, schema, table = split_postgres_url(spec)
        # libpq must not see /schema/table in the URL - rebuild cleanly.
        parts = urlsplit(spec)
        clean_url = f"{parts.scheme}://{parts.netloc}/{_dbname}"
        if parts.query:
            clean_url += f"?{parts.query}"
        qualified = table if schema is None else f"{schema}.{table}"
        return "postgres", None, clean_url, qualified

    if spec.startswith("duckdb://"):
        rest = spec[len("duckdb://") :]
        if not rest or rest.endswith(".duckdb"):
            msg = f"invalid duckdb spec {spec!r}; expected duckdb://<path>/<table>"
            raise SourceError(msg)
        path_str, sep, table = rest.rpartition("/")
        if not sep or not path_str or not table:
            msg = f"invalid duckdb spec {spec!r}; expected duckdb://<path>/<table>"
            raise SourceError(msg)
        return "duckdb", Path(path_str), None, table

    p = Path(spec)
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        return "parquet", p, None, None
    if suffix in {".csv", ".tsv", ".txt"}:
        return "csv", p, None, None
    msg = (
        f"cannot interpret source {spec!r}: unknown scheme or file extension. "
        "Supported: *.parquet, *.csv, duckdb://path/db.duckdb/table, postgres://host/db/schema/table"
    )
    raise SourceError(msg)


def bind_source(
    session: object,
    alias: str,
    spec: str,
    options: SourceOptions | None = None,
) -> BoundSource:
    """Bind a source spec under an alias in the given session."""
    opts = options or SourceOptions()
    # Imported lazily to keep module import cheap; Session is duck-typed here
    # because duckdb itself is untyped.
    from tabdiff.session import Session  # noqa: PLC0415

    assert isinstance(session, Session)
    kind, path, url, table = parse_source_spec(spec)

    if kind in {"parquet", "csv"}:
        assert path is not None
        return bind_file(session, alias, path, all_varchar=opts.all_varchar)

    if kind == "duckdb":
        assert path is not None and table is not None
        return DuckDBFileSource(session, alias, path, table)

    assert url is not None and table is not None
    schema: str | None = None
    if "." in table:
        schema, _, table = table.partition(".")
    from tabdiff.source.postgres import PostgresSource  # noqa: PLC0415

    return PostgresSource(session, alias, url, table, schema=schema)


__all__ = ["SourceOptions", "bind_source", "parse_source_spec"]
