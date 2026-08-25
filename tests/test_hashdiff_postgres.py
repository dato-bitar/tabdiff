"""M6 cross-engine acceptance: Postgres hashdiff parity (spec-mandated).

The classic failure mode of data-diff tools: checksums computed with
different semantics on each side report differences everywhere. These tests
pin tabdiff to the opposite behaviour: an identical table in Postgres and in
Parquet must produce ZERO differences, and injected deviations must be found
completely - across engines.
"""

from __future__ import annotations

from typing import Any

import pyarrow.parquet as pq
import pytest

from tabdiff.canon import CompareOptions
from tabdiff.session import Session
from tabdiff.source import bind_source
from tabdiff.source.postgres import PostgresSource
from tabdiff.strategy.hash_diff import run_hash_diff
from tests.gen import build, make_base
from tests.pg_utils import load_arrow_into_pg, pg_source_spec

pytestmark = pytest.mark.postgres


@pytest.fixture()
def session() -> Any:
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def pg_pair(pg_dsn: str, tmp_path: Any) -> tuple[str, str]:
    """Identical data as a postgres table AND a parquet file."""
    name = "tabdiff_m6_parity"
    setup = Session()
    try:
        load_arrow_into_pg(setup, pg_dsn, name, make_base(n_rows=800, seed=77))
        spec = pg_source_spec(pg_dsn, name)
        src = bind_source(setup, "tmp", spec)
        arrow = setup.arrow(f"SELECT * FROM {src.relation_sql()}")
    finally:
        setup.close()
    ppath = tmp_path / "parity.parquet"
    pq.write_table(arrow, ppath)
    return spec, str(ppath)


class TestCrossEngineParity:
    def test_identical_zero_diffs(self, session: Session, pg_pair: tuple[str, str]) -> None:
        spec, pp = pg_pair
        src_l = bind_source(session, "l", spec)
        src_r = bind_source(session, "r", pp)
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=64
        )
        assert report.identical, (
            f"cross-engine parity broken: counts={report.counts} "
            f"values={[(c.column, c.mismatched_rows) for c in report.values.columns]} "
            f"examples={[c.examples[:2] for c in report.values.columns]}"
        )


class TestPushdownPaths:
    """The pushdown vs local-scan choice must be visible and both paths correct."""

    def test_pushdown_active_and_zero_diffs(
        self, session: Session, pg_pair: tuple[str, str]
    ) -> None:
        spec, pp = pg_pair
        src_l = bind_source(session, "l", spec)
        assert isinstance(src_l, PostgresSource)
        assert src_l._check_pushdown() is True, (
            "postgres_query pushdown expected with the installed postgres_scanner; "
            "if this fails the extension lost the postgres_query(alias, sql) form"
        )
        src_r = bind_source(session, "r", pp)
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=64
        )
        assert report.identical, report.warnings
        assert report.meta.execution_path == {"left": "pushdown", "right": "local-scan"}

    def test_pushdown_injected_changes_found(
        self, session: Session, pg_dsn: str, tmp_path: Any
    ) -> None:
        inj = build("value_changed", n_rows=400, seed=91)
        name = "tabdiff_m6_pd_inject"
        load_arrow_into_pg(Session(), pg_dsn, name, inj.left)
        spec = pg_source_spec(pg_dsn, name)
        ppath = tmp_path / "pd.parquet"
        pq.write_table(inj.right, ppath)

        src_l = bind_source(session, "l", spec)
        assert isinstance(src_l, PostgresSource)
        assert src_l.preferred_engine() == "postgres"
        src_r = bind_source(session, "r", str(ppath))
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=64
        )
        got = {c.column: c.mismatched_rows for c in report.values.columns}
        assert got == {"score": inj.expected.cells["score"]}, got
        assert report.meta.execution_path["left"] == "pushdown"

    def test_forced_fallback_zero_diffs(
        self,
        session: Session,
        pg_pair: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec, pp = pg_pair
        src_l = bind_source(session, "l", spec)
        assert isinstance(src_l, PostgresSource)

        def _no_pushdown() -> bool:
            return False

        monkeypatch.setattr(src_l, "_check_pushdown", _no_pushdown)
        assert src_l.preferred_engine() == "duckdb"
        src_r = bind_source(session, "r", pp)
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=64
        )
        assert report.identical, report.warnings
        assert report.meta.execution_path["left"] == "local-scan"

    def test_forced_fallback_finds_injections(
        self,
        session: Session,
        pg_dsn: str,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inj = build("value_changed", n_rows=400, seed=92)
        name = "tabdiff_m6_fb_inject"
        load_arrow_into_pg(Session(), pg_dsn, name, inj.left)
        spec = pg_source_spec(pg_dsn, name)
        ppath = tmp_path / "fb.parquet"
        pq.write_table(inj.right, ppath)

        src_l = bind_source(session, "l", spec)
        assert isinstance(src_l, PostgresSource)

        def _no_pushdown() -> bool:
            return False

        monkeypatch.setattr(src_l, "_check_pushdown", _no_pushdown)
        src_r = bind_source(session, "r", str(ppath))
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=64
        )
        got = {c.column: c.mismatched_rows for c in report.values.columns}
        assert got == {"score": inj.expected.cells["score"]}, got
        assert report.meta.execution_path["left"] == "local-scan"

    def test_injected_changes_found(self, session: Session, pg_dsn: str, tmp_path: Any) -> None:
        inj = build("value_changed", n_rows=600, seed=41)
        name = "tabdiff_m6_injected"
        load_arrow_into_pg(Session(), pg_dsn, name, inj.left)
        spec = pg_source_spec(pg_dsn, name)
        ppath = tmp_path / "mut.parquet"
        pq.write_table(inj.right, ppath)

        src_l = bind_source(session, "l", spec)
        src_r = bind_source(session, "r", str(ppath))
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=64
        )
        got = {c.column: c.mismatched_rows for c in report.values.columns}
        assert got == {"score": inj.expected.cells["score"]}, got
        assert report.counts.left_only == 0
        assert report.counts.right_only == 0

    def test_row_moved_between_sides_found(
        self, session: Session, pg_dsn: str, tmp_path: Any
    ) -> None:
        """A row deleted from PG and added to parquet must be found."""
        inj = build("row_added", n_rows=500, seed=17)
        name = "tabdiff_m6_move"
        load_arrow_into_pg(Session(), pg_dsn, name, inj.left)
        spec = pg_source_spec(pg_dsn, name)
        ppath = tmp_path / "moved.parquet"
        pq.write_table(inj.right, ppath)

        src_l = bind_source(session, "l", spec)
        src_r = bind_source(session, "r", str(ppath))
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=32
        )
        assert report.counts.right_only == inj.expected.rows_only_right
        assert report.counts.left_only == 0
