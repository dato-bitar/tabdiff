"""A3 battle tests: realistic dirty schemas must not crash or miscount.

Each injection from gen.py already runs against BOTH strategies via the
parametrized suites; these tests pin the *specific* expectations of the
dirty-schema injections (name handling, separation, scale) that generic
assertions cannot express.
"""

from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from tabdiff.canon import CompareOptions
from tabdiff.session import Session
from tabdiff.source import bind_source
from tabdiff.strategy.hash_diff import run_hash_diff
from tabdiff.strategy.join_diff import run_join_diff
from tests.gen import build


@pytest.fixture()
def session() -> Session:
    s = Session()
    yield s
    s.close()


def write_pair(tmp_dir, inj) -> tuple[str, str]:
    lp = tmp_dir / "left.parquet"
    rp = tmp_dir / "right.parquet"
    pq.write_table(inj.left, lp)
    pq.write_table(inj.right, rp)
    return str(lp), str(rp)


def run_both(session: Session, lp: str, rp: str, tmp_dir, *, leaf_rows: int = 64):
    jl = run_join_diff(
        session,
        bind_source(session, "l", lp),
        bind_source(session, "r", rp),
        key_cols=["id"],
        opts=CompareOptions(),
    )
    hr = run_hash_diff(
        session,
        bind_source(session, "hl", lp),
        bind_source(session, "hr", rp),
        key_cols=["id"],
        opts=CompareOptions(),
        leaf_rows=leaf_rows,
    )
    return jl, hr


class TestColumnNameCaseSpace:
    def test_reported_as_missing_never_matched(self, session: Session, tmp_path) -> None:
        inj = build("column_name_case_space", n_rows=200, seed=5)
        lp, rp = write_pair(tmp_path, inj)
        jl, hr = run_both(session, lp, rp, tmp_path)
        for report in (jl, hr):
            statuses = {c.name: c.status for c in report.schema.columns}
            assert statuses["User ID"] == "only_left", statuses
            assert statuses["user_id"] == "only_right", statuses
            assert not report.values.columns
            assert report.counts.left_only == 0 and report.counts.right_only == 0


class TestSpecialCharColumnNames:
    def test_identical_values_zero_diffs(self, session: Session, tmp_path) -> None:
        inj = build("special_char_column_names", n_rows=200, seed=6)
        lp, rp = write_pair(tmp_path, inj)
        jl, hr = run_both(session, lp, rp, tmp_path)
        assert jl.identical, jl.values
        assert hr.identical, hr.values


class TestCaseCollidingColumns:
    def test_columns_stay_separate_and_changes_attributed(self, session: Session, tmp_path) -> None:
        inj = build("case_colliding_columns", n_rows=300, seed=8)
        lp, rp = write_pair(tmp_path, inj)
        jl, hr = run_both(session, lp, rp, tmp_path)
        for report in (jl, hr):
            got = {c.column: c.mismatched_rows for c in report.values.columns}
            # changes live ONLY in 'Delta' (duckdb keeps the first case-variant;
            # the second becomes 'delta_1'); nothing may leak into 'delta_1'
            assert got == {"Delta": 3}, got
            names = {c.name for c in report.schema.columns}
            assert {"Delta", "delta_1"} <= names, names


class TestLongStrings:
    def test_ten_kb_cells_counted_exactly(self, session: Session, tmp_path) -> None:
        inj = build("long_strings", n_rows=250, seed=9)
        lp, rp = write_pair(tmp_path, inj)
        jl, hr = run_both(session, lp, rp, tmp_path)
        for report in (jl, hr):
            got = {c.column: c.mismatched_rows for c in report.values.columns}
            assert got == {"long_text": 4}, got


class TestMostlyNulls:
    def test_rare_nonnull_changes_found(self, session: Session, tmp_path) -> None:
        inj = build("mostly_nulls", n_rows=400, seed=10)
        lp, rp = write_pair(tmp_path, inj)
        jl, hr = run_both(session, lp, rp, tmp_path)
        for report in (jl, hr):
            got = {c.column: c.mismatched_rows for c in report.values.columns}
            assert got == {"sparse": 3}, got
