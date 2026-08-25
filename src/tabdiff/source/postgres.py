"""Postgres source, reached through DuckDB's postgres_scanner extension."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from tabdiff.errors import SourceError
from tabdiff.session import quote_literal
from tabdiff.source.base import ColumnInfo, RelationSource, describe_to_columns

if TYPE_CHECKING:
    from tabdiff.session import Session


def split_postgres_url(url: str) -> tuple[str, str, str | None, str]:
    """Split ``postgres://host/db[/schema]/table`` into parts.

    Returns ``(conninfo, dbname, schema_or_None, table)``. The conninfo is the
    original URL (libpq accepts URL form). ``schema`` is None when
    unspecified; callers fall back to their default policy.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql"}:
        msg = f"not a postgres URL: {url!r}"
        raise SourceError(msg)
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if not segments:
        msg = f"postgres URL {url!r} must include a database name"
        raise SourceError(msg)
    if len(segments) < 2:
        msg = f"postgres URL {url!r} must include a table name (postgres://host/db/schema/table)"
        raise SourceError(msg)
    conninfo = url
    dbname = segments[0]
    if len(segments) == 2:
        return conninfo, dbname, None, segments[1]
    *middle, table = segments
    schema = middle[-1]
    return conninfo, dbname, schema, table


class PostgresSource(RelationSource):
    """A table inside an attached Postgres database.

    Beyond a plain relation this source knows its engine (``postgres``), so
    hashdiff can push checksum aggregates down via ``execute_remote`` using
    the ``postgres_query`` table function when the installed extension
    supports it.
    """

    DEFAULT_SCHEMA = "public"

    def __init__(
        self,
        session: Session,
        alias: str,
        url: str,
        table: str,
        schema: str | None = None,
    ) -> None:
        session.attach_postgres(url, alias)
        self.url = url
        self.schema_was_explicit = schema is not None
        self.pg_schema = schema if schema is not None else self.DEFAULT_SCHEMA
        self.pg_table = table
        qualified = f'"{alias}"."{self.pg_schema}"."{table.replace(chr(34), chr(34) * 2)}"'
        try:
            cols = describe_to_columns(session.rows(f"DESCRIBE {qualified}"))
        except Exception as exc:
            if not self.schema_was_explicit:
                msg = (
                    f"table {table!r} not found in default schema "
                    f"{self.DEFAULT_SCHEMA!r}; specify it as "
                    ".../db/schema/table in the URL"
                )
                raise SourceError(msg) from exc
            msg = f"table {self.pg_schema}.{table!r} not reachable in postgres: {exc}"
            raise SourceError(msg) from exc
        super().__init__(session, alias, qualified, declared=self._declared_from_catalog(cols))
        self._pushdown_ok: bool | None = None

    @property
    def engine(self) -> str:
        return "postgres"

    # -- catalog metadata ----------------------------------------------------

    def _declared_from_catalog(self, fallback: list[ColumnInfo]) -> list[ColumnInfo]:
        """Use catalog nullability when available; types stay DuckDB's mapping."""
        qschema, qtable = quote_literal(self.pg_schema), quote_literal(self.pg_table)
        sql = (
            "SELECT column_name, is_nullable "
            f'FROM "{self.alias}"."information_schema"."columns" '
            f"WHERE table_schema = {qschema} AND table_name = {qtable} "
            "ORDER BY ordinal_position"
        )
        try:
            rows = self.session.rows(sql)
        except Exception:
            return fallback
        if not rows:
            return fallback
        by_name = {str(r[0]): str(r[1]).upper() == "YES" for r in rows}
        return [
            ColumnInfo(name=c.name, type=c.type, nullable=by_name.get(c.name, c.nullable))
            for c in fallback
        ]

    def primary_key_hint(self) -> list[str]:
        qschema, qtable = quote_literal(self.pg_schema), quote_literal(self.pg_table)
        sql = (
            "SELECT kcu.column_name "
            f'FROM "{self.alias}"."information_schema"."table_constraints" tc '
            f'JOIN "{self.alias}"."information_schema"."key_column_usage" kcu '
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            f"AND tc.table_schema = {qschema} AND tc.table_name = {qtable} "
            "ORDER BY kcu.ordinal_position"
        )
        try:
            return [str(r[0]) for r in self.session.rows(sql)]
        except Exception:
            return []

    # -- remote execution ------------------------------------------------------

    def _check_pushdown(self) -> bool:
        if self._pushdown_ok is None:
            try:
                self.session.scalar(
                    f"SELECT 1 FROM postgres_query({quote_literal(self.url)}, 'SELECT 1')"
                )
                self._pushdown_ok = True
            except Exception:
                self._pushdown_ok = False
        return self._pushdown_ok

    def remote_sql(self, template: str, **params: str) -> str:
        """Render a SQL template containing ``{relation}`` against this source."""
        return template.format(relation=self.relation_sql(), **params)

    def execute_remote(self, sql: str) -> Any:
        """Run a SELECT close to the data when possible, else scan locally.

        Returns an Arrow Table either way. If the pushdown path fails at
        runtime (extension without postgres_query support, unsupported SQL),
        we degrade to running the same statement against the attached table
        through DuckDB - correct, but moves rows over the wire.
        """
        if self._check_pushdown():
            wrapped = (
                f"SELECT * FROM postgres_query({quote_literal(self.url)}, {quote_literal(sql)})"
            )
            try:
                return self.session.arrow(wrapped)
            except Exception:
                self._pushdown_ok = False
        return self.session.arrow(sql)


__all__ = ["PostgresSource", "split_postgres_url"]
