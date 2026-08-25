"""M6 acceptance: hashdiff - checksums per side, bisect, pull only diffs."""

from __future__ import annotations

from typing import Any

import duckdb as duckdb_mod
import pyarrow.parquet as pq
import pytest

from tabdiff.canon import CompareOptions
from tabdiff.errors import KeyNotUnique
from tabdiff.session import Session
from tabdiff.source import bind_source
from tabdiff.strategy.hash_diff import DEFAULT_LEAF_ROWS, hex_prefix_to_int_sql, run_hash_diff
from tests.gen import ALL_INJECTIONS, build


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


def diff_files(session: Session, lp: str, rp: str, **kw: Any):
    opts = kw.pop("opts", CompareOptions())
    src_l = bind_source(session, "l", lp)
    src_r = bind_source(session, "r", rp)
    return run_hash_diff(
        session,
        src_l,
        src_r,
        key_cols=list(kw.pop("key", ("id",))),
        opts=opts,
        **kw,
    )


def _assert_expected(report, expected) -> None:
    assert report.counts is not None
    assert report.counts.left_only == expected.rows_only_left, report.counts
    assert report.counts.right_only == expected.rows_only_right, report.counts
    got = {c.column: c.mismatched_rows for c in report.values.columns}
    for col, n in expected.cells.items():
        assert got.get(col) == n, f"column {col}: want {n}, got {got}"
    for col in got:
        assert col in expected.cells, f"unexpected diffs in {col}: {got}"


class TestChecksumPrimitives:
    def test_hex_prefix_arithmetic_matches_python(self, session: Session) -> None:
        samples = [
            "0123456789abcdef0123456789abcdef",
            "ffffffffffffffffffffffffffffffff",
            "deadbeefdeadbeefdeadbeefdeadbeef",
            "00000000000000000000000000000000",
        ]
        for h in samples:
            expr = hex_prefix_to_int_sql("h", 15, 0)
            row = session.execute(f"SELECT {expr} FROM (SELECT '{h}' AS h)").fetchone()
            assert row[0] == int(h[:15], 16)
            expr2 = hex_prefix_to_int_sql("h", 15, 15)
            row2 = session.execute(f"SELECT {expr2} FROM (SELECT '{h}' AS h)").fetchone()
            assert row2[0] == int(h[15:30], 16)

    def test_too_many_digits_rejected(self) -> None:
        with pytest.raises(ValueError, match="15"):
            hex_prefix_to_int_sql("h", 16)


@pytest.mark.parametrize("kind", [k for k in ALL_INJECTIONS if k != "duplicate_key_introduced"])
class TestInjectionsDetectedExactly:
    def test_hash_diff(self, kind: str, session: Session, tmp_path: Any) -> None:
        inj = build(kind, n_rows=400, seed=11)
        lp, rp = write_pair(tmp_path, inj)
        # small leaf size forces real bisecting instead of a single pull
        report = diff_files(session, lp, rp, leaf_rows=64)
        _assert_expected(report, inj.expected)

    def test_hash_matches_join_semantics(self, kind: str, session: Session, tmp_path: Any) -> None:
        from tabdiff.strategy.join_diff import run_join_diff

        inj = build(kind, n_rows=250, seed=21)
        lp, rp = write_pair(tmp_path, inj)
        hr = diff_files(session, lp, rp, leaf_rows=DEFAULT_LEAF_ROWS)
        jl = run_join_diff(
            session,
            bind_source(session, "l", lp),
            bind_source(session, "r", rp),
            key_cols=["id"],
            opts=CompareOptions(),
        )
        assert hr.identical == jl.identical
        gh = {c.column: c.mismatched_rows for c in hr.values.columns}
        gj = {c.column: c.mismatched_rows for c in jl.values.columns}
        assert gh == gj, f"hash={gh} join={gj}"


class TestBisectBehaviour:
    def test_identical_tables_zero_diffs_multi_level(self, session: Session, tmp_path: Any) -> None:
        inj = build("order_shuffled", n_rows=3000, seed=5)
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp, leaf_rows=128)
        assert report.identical, f"{report.counts} {report.values}"
        assert report.meta.strategy == "hash"

    def test_duplicate_key_aborts(self, session: Session, tmp_path: Any) -> None:
        inj = build("duplicate_key_introduced", n_rows=100)
        lp, rp = write_pair(tmp_path, inj)
        with pytest.raises(KeyNotUnique):
            diff_files(session, lp, rp)

    def test_sparse_changes_across_buckets(self, session: Session, tmp_path: Any) -> None:
        """Changes scattered over many buckets must all be found."""
        import random

        import pyarrow as pa

        inj = build("value_changed", n_rows=500, seed=7)
        rng = random.Random(99)
        tbl = inj.right
        scores = tbl.column("score").to_pylist()
        changed_extra = 0
        for i in rng.sample(range(500), 40):
            if scores[i] is not None:
                scores[i] += 3.25
                changed_extra += 1
        tbl = tbl.set_column(
            tbl.schema.get_field_index("score"),
            "score",
            pa.array(scores, type=pa.float64()),
        )
        inj.right = tbl
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp, leaf_rows=32)
        got = {c.column: c.mismatched_rows for c in report.values.columns}
        # 7 injected by value_changed(seed=7) + extra scattered changes (some
        # may coincide with the original ones)
        assert got["score"] >= 40, got
        assert report.counts.left_only == 0 and report.counts.right_only == 0


class TestSeparateEngines:
    def test_two_duckdb_files_as_two_engines(self, session: Session, tmp_path: Any) -> None:
        """Simulates 'two isolated sources': each side its own database file."""
        inj = build("value_changed", n_rows=300, seed=31)
        d1 = tmp_path / "a.duckdb"
        d2 = tmp_path / "b.duckdb"
        con1 = duckdb_mod.connect(str(d1))
        con1.register("t", inj.left)
        con1.execute("CREATE TABLE events AS SELECT * FROM t")
        con1.close()
        con2 = duckdb_mod.connect(str(d2))
        con2.register("t", inj.right)
        con2.execute("CREATE TABLE events AS SELECT * FROM t")
        con2.close()

        src_l = bind_source(session, "la", f"duckdb://{d1.as_posix()}/events")
        src_r = bind_source(session, "ra", f"duckdb://{d2.as_posix()}/events")
        report = run_hash_diff(
            session, src_l, src_r, key_cols=["id"], opts=CompareOptions(), leaf_rows=50
        )
        got = {c.column: c.mismatched_rows for c in report.values.columns}
        assert got == {"score": inj.expected.cells["score"]}
        assert report.counts.left_only == 0
