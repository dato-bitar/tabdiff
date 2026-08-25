"""Postgres integration: binding, roundtrip, catalog metadata.

Requires Docker (testcontainers) or TABDIFF_TEST_PG_DSN; skips otherwise.
"""

from __future__ import annotations

from typing import Any

import pyarrow.parquet as pq
import pytest

from tabdiff.canon import CompareOptions
from tabdiff.errors import SourceError
from tabdiff.session import Session
from tabdiff.source import bind_source
from tests.gen import make_base
from tests.pg_utils import load_arrow_into_pg, pg_source_spec

pytestmark = pytest.mark.postgres


@pytest.fixture()
def session() -> Any:
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def pg_table(pg_dsn: str) -> str:
    name = "tabdiff_m5_roundtrip"
    load_arrow_into_pg(Session(), pg_dsn, name, make_base(n_rows=300, seed=13))
    return name


class TestBinding:
    def test_bind_count_schema(self, session: Any, pg_dsn: str, pg_table: str) -> None:
        src = bind_source(session, "pg", pg_source_spec(pg_dsn, pg_table))
        assert src.count() == 300
        types = {c.name: c.type for c in src.columns()}
        assert types["id"] == "BIGINT"
        assert types["amount"] == "DECIMAL(18,4)"
        assert types["flag"] == "BOOLEAN"
        assert types["created_at"] == "TIMESTAMP"
        # nullability comes from the catalog for postgres sources
        nullable = {c.name: c.nullable for c in src.declared_columns()}
        # CTAS produces all-nullable columns in postgres - both must be True
        assert all(nullable.values())

    def test_missing_table_raises(self, session: Any, pg_dsn: str) -> None:
        with pytest.raises(SourceError, match="not reachable"):
            bind_source(session, "pg", pg_source_spec(pg_dsn, "no_such_table_xyz"))

    def test_engine_is_postgres(self, session: Any, pg_dsn: str, pg_table: str) -> None:
        src = bind_source(session, "pg", pg_source_spec(pg_dsn, pg_table))
        assert src.engine == "postgres"


class TestRoundtrip:
    def test_pg_vs_parquet_identical_joindiff(
        self, session: Any, pg_dsn: str, pg_table: str, tmp_path: Any
    ) -> None:
        """The canonical zero-noise check: same data in PG and parquet."""
        from tabdiff.strategy.join_diff import run_join_diff

        spec = pg_source_spec(pg_dsn, pg_table)
        src = bind_source(session, "pg", spec)
        arrow = session.arrow(f"SELECT * FROM {src.relation_sql()}")
        ppath = tmp_path / "roundtrip.parquet"
        pq.write_table(arrow, ppath)

        src_l = bind_source(session, "l", spec)
        src_r = bind_source(session, "r", str(ppath))
        report = run_join_diff(session, src_l, src_r, key_cols=["id"], opts=CompareOptions())
        assert report.identical, (
            f"roundtrip must be identical: counts={report.counts} "
            f"values={report.values} warnings={report.meta.warnings}"
        )

    def test_pk_hint_detected(self, session: Any, pg_dsn: str) -> None:
        import contextlib

        con = session.con
        session.attach_postgres(pg_dsn, "pgpk")
        with contextlib.suppress(Exception):
            con.execute("DROP TABLE IF EXISTS pgpk.public.tabdiff_m5_pk")
        # create via raw SQL through a pushdown query if possible, else scan
        con.execute('CREATE TABLE pgpk.public."tabdiff_m5_pk" (id BIGINT PRIMARY KEY, v VARCHAR)')
        src = bind_source(session, "pg2", pg_source_spec(pg_dsn, "tabdiff_m5_pk"))
        assert src.primary_key_hint() == ["id"]
