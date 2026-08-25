"""M7: column-statistics drift, keyless mode, orchestrator behaviour."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tabdiff.canon import CompareOptions
from tabdiff.diff import RunOptions, choose_strategy, run_diff
from tabdiff.session import Session
from tabdiff.source import bind_source
from tabdiff.stats import compute_stats_drift
from tabdiff.strategy.keyless import run_keyless_diff
from tests.gen import build


@pytest.fixture()
def session() -> Session:
    s = Session()
    yield s
    s.close()


def _pair(tmp_path: Any, l_tbl: pa.Table, r_tbl: pa.Table) -> tuple[str, str]:
    lp, rp = tmp_path / "l.parquet", tmp_path / "r.parquet"
    pq.write_table(l_tbl, lp)
    pq.write_table(r_tbl, rp)
    return str(lp), str(rp)


class TestStatsDrift:
    def test_stats_match_known_values(self, session: Session, tmp_path: Any) -> None:
        l_tbl = pa.table(
            {
                "id": pa.array([1, 2, 3, 4], type=pa.int64()),
                "v": pa.array([10.0, 20.0, None, 40.0], type=pa.float64()),
                "s": pa.array(["a", "b", None, "a"]),
            }
        )
        r_tbl = pa.table(
            {
                "id": pa.array([1, 2, 3, 4], type=pa.int64()),
                "v": pa.array([11.0, 21.0, None, 41.0], type=pa.float64()),
                "s": pa.array(["x", "y", None, "x"]),
            }
        )
        lp, rp = _pair(tmp_path, l_tbl, r_tbl)
        l = bind_source(session, "l", lp)
        r = bind_source(session, "r", rp)
        stats = compute_stats_drift(
            session,
            l,
            r,
            {
                "id": __import__("tabdiff").normalize.Canon.INTEGER,
                "v": __import__("tabdiff").normalize.Canon.FLOAT,
                "s": __import__("tabdiff").normalize.Canon.STRING,
            },
        )
        lv, rv = stats.columns["v"]
        assert lv.null_count == 1 and rv.null_count == 1
        assert lv.min_ == "10.0" and rv.min_ == "11.0"
        assert lv.distinct_count == 3 and rv.distinct_count == 3
        ls, rs = stats.columns["s"]
        assert ls.null_count == 1 and rs.null_count == 1
        # avg is numeric-only: strings get None
        assert ls.avg is None and rs.avg is None

    def test_stats_attached_by_run_diff(self, session: Session, tmp_path: Any) -> None:
        inj = build("value_changed", n_rows=200)
        lp, rp = _pair(tmp_path, inj.left, inj.right)
        report = run_diff(lp, rp, RunOptions(include_stats=True), session=session)
        assert report.stats is not None
        assert "score" in report.stats.columns
        left_s, right_s = report.stats.columns["score"]
        # value_changed adds +17.5 to n scores -> averages differ
        assert left_s.avg != right_s.avg

    def test_no_stats_option(self, session: Session, tmp_path: Any) -> None:
        inj = build("order_shuffled", n_rows=50)
        lp, rp = _pair(tmp_path, inj.left, inj.right)
        report = run_diff(lp, rp, RunOptions(include_stats=False), session=session)
        assert report.stats is None
        assert report.identical


class TestKeyless:
    def test_keyless_finds_added_removed(self, session: Session, tmp_path: Any) -> None:
        inj_add = build("row_added", n_rows=100, seed=5)
        inj_del = build("row_deleted", n_rows=100, seed=5)

        for inj, side, expected_n in (
            (inj_add, "right_only", 3),
            (inj_del, "left_only", 3),
        ):
            sub = tmp_path / f"k{side}"
            sub.mkdir(exist_ok=True)
            lp, rp = _pair(sub, inj.left, inj.right)
            l = bind_source(session, "kl", lp)
            r = bind_source(session, "kr", rp)
            rep = run_keyless_diff(session, l, r, opts=CompareOptions())
            counts = rep.counts
            assert counts is not None and counts.both == 100 - (3 if side == "left_only" else 0)
            got = getattr(counts, side)
            assert got == expected_n, f"{side}: {got}"
            assert rep.meta.strategy == "keyless"
            assert any("WHICH CELL" in w for w in rep.meta.warnings)

    def test_keyless_multiset_semantics(self, session: Session, tmp_path: Any) -> None:
        """Same rows in different order -> identical under key-less."""
        inj = build("order_shuffled", n_rows=80)
        lp, rp = _pair(tmp_path, inj.left, inj.right)
        l = bind_source(session, "ml", lp)
        r = bind_source(session, "mr", rp)
        rep = run_keyless_diff(session, l, r, opts=CompareOptions())
        assert rep.counts is not None and rep.counts.left_only == 0
        assert rep.counts.right_only == 0


class TestStrategyChoice:
    def test_auto_local_is_join(self, tmp_path: Any) -> None:
        p = tmp_path / "x.parquet"
        pq.write_table(pa.table({"id": pa.array([1])}), p)
        s = Session()
        try:
            l = bind_source(s, "l", str(p))
            r = bind_source(s, "r", str(p))
            assert choose_strategy(l, r, RunOptions()) == "join"
        finally:
            s.close()

    def test_forced_hash_on_local_files(self, tmp_path: Any) -> None:
        p = tmp_path / "x.parquet"
        pq.write_table(pa.table({"id": pa.array([1])}), p)
        s = Session()
        try:
            l = bind_source(s, "l", str(p))
            r = bind_source(s, "r", str(p))
            assert choose_strategy(l, r, RunOptions(strategy="hash")) == "hash"
        finally:
            s.close()

    def test_run_diff_end_to_end_join(self, session: Session, tmp_path: Any) -> None:
        inj = build("value_changed", n_rows=150)
        lp, rp = _pair(tmp_path, inj.left, inj.right)
        report = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        assert report.meta.strategy == "join"
        cells = {c.column: c.mismatched_rows for c in report.values.columns}
        assert cells == {"score": inj.expected.cells["score"]}
