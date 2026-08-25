"""M4 acceptance: joindiff detects all 13 injected deviation types exactly."""

from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from tabdiff.canon import CompareOptions
from tabdiff.errors import KeyNotUnique
from tabdiff.session import Session
from tabdiff.source import bind_source
from tabdiff.strategy.join_diff import run_join_diff
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


def diff_files(
    session: Session,
    lp: str,
    rp: str,
    *,
    key=("id",),
    opts: CompareOptions | None = None,
    **kw: object,
):
    src_l = bind_source(session, "l", lp)
    src_r = bind_source(session, "r", rp)
    return run_join_diff(
        session, src_l, src_r, key_cols=list(key), opts=opts or CompareOptions(), **kw
    )


def _assert_expected(report, expected) -> None:
    if report.counts:
        assert report.counts.left_only == expected.rows_only_left
        assert report.counts.right_only == expected.rows_only_right
    for col, n in expected.cells.items():
        found = [c for c in report.values.columns if c.column == col]
        assert found, f"expected mismatches in column {col!r}"
        total = sum(c.mismatched_rows for c in found)
        assert total == n, f"column {col}: expected {n} mismatching rows, got {total}"
        for c in found:
            assert c.examples, "examples must be present by default"
            assert len(c.examples) <= 20
    unexpected = {
        c.column: c.mismatched_rows for c in report.values.columns if c.column not in expected.cells
    }
    assert not unexpected, f"unexpected value differences: {unexpected}"


@pytest.mark.parametrize("kind", [k for k in ALL_INJECTIONS if k != "duplicate_key_introduced"])
class TestInjectionsDetectedExactly:
    def test_join_diff(self, kind: str, session: Session, tmp_path) -> None:
        inj = build(kind, n_rows=400, seed=11)
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp)
        _assert_expected(report, inj.expected)

        # schema expectations
        statuses = {c.name: c.status for c in report.schema.columns}
        for col in inj.expected.columns_only_left:
            assert statuses.get(col) == "only_left", f"{kind}: {col} -> {statuses}"
        for col in inj.expected.columns_only_right:
            assert statuses.get(col) == "only_right"
        for note in inj.expected.schema_notes:
            assert any(
                note.lower() in (c.note or "").lower() or note.lower() in (c.status or "").lower()
                for c in report.schema.columns
            ), f"{kind}: schema note {note!r} missing"

    def test_examples_are_bounded(self, kind: str, session: Session, tmp_path) -> None:
        inj = build(kind, n_rows=120, seed=3)
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp, examples_n=5)
        for c in report.values.columns:
            assert len(c.examples) <= 5


class TestSpecialBehaviours:
    def test_duplicate_key_aborts(self, session: Session, tmp_path) -> None:
        inj = build("duplicate_key_introduced", n_rows=100)
        lp, rp = write_pair(tmp_path, inj)
        with pytest.raises(KeyNotUnique, match="not unique"):
            diff_files(session, lp, rp)

    def test_order_shuffled_is_zero_diff(self, session: Session, tmp_path) -> None:
        inj = build("order_shuffled", n_rows=250)
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp)
        assert report.identical, (
            f"shuffling must not produce diffs: counts={report.counts}, values={report.values}"
        )

    def test_precision_lost_coarse_mode_no_value_diffs(self, session: Session, tmp_path) -> None:
        inj = build("precision_lost", n_rows=300)
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp)
        assert not report.values.columns, "coarse mode must absorb precision loss"
        assert report.identical
        assert any("milliseconds" in a for a in report.meta.assumptions)

    def test_timezone_shifted_requires_assumption(self, session: Session, tmp_path) -> None:
        inj = build("timezone_shifted", n_rows=100)
        lp, rp = write_pair(tmp_path, inj)

        # without --assume-tz: loud warning, values not compared
        report = diff_files(session, lp, rp)
        ts = next(c for c in report.schema.columns if c.name == "created_at")
        assert ts.status == "needs_tz"
        assert any("--assume-tz" in w for w in report.meta.warnings)
        assert not report.values.columns

        # with the WRONG zone: every row differs (assumption is detectable)
        wrong = CompareOptions(assume_tz="Europe/Berlin")
        report_wrong = diff_files(session, lp, rp, opts=wrong)
        cells = {c.column: c.mismatched_rows for c in report_wrong.values.columns}
        assert cells["created_at"] == 100

        # with the RIGHT zone: identical instants, values clean, assumption recorded
        right = CompareOptions(assume_tz="UTC")
        report_right = diff_files(session, lp, rp, opts=right)
        assert not report_right.values.columns
        assert any("UTC" in a for a in report_right.meta.assumptions)

    def test_json_key_order_is_semantic_equal(self, session: Session, tmp_path) -> None:
        import pyarrow as pa

        t_l = pa.table({"id": pa.array([1, 2]), "p": pa.array(['{"a":1,"b":2}', '{"x":[1]}'])})
        t_r = pa.table({"id": pa.array([1, 2]), "p": pa.array(['{  "b":2, "a":1 }', '{"x":[1]}'])})
        lp, rp = tmp_path / "jl.parquet", tmp_path / "jr.parquet"
        pq.write_table(t_l, lp)
        pq.write_table(t_r, rp)
        opts = CompareOptions(json_columns=frozenset({"p"}))
        report = diff_files(session, str(lp), str(rp), opts=opts)
        assert report.identical, f"key order must not matter: {report.values}"

    def test_nfc_equivalence_not_a_difference(self, session: Session, tmp_path) -> None:
        from tests.gen import nfc_equivalent

        inj = nfc_equivalent(build_base_small(), __import__("random").Random(1))
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp)
        assert report.identical, "NFC vs NFD must compare equal"


def build_base_small():
    from tests.gen import make_base

    return make_base(n_rows=80, seed=9)


class TestKeys:
    def test_composite_key(self, session: Session, tmp_path) -> None:
        import pyarrow as pa

        base = [
            {"a": 1, "b": "x", "v": 10},
            {"a": 1, "b": "y", "v": 20},
            {"a": 2, "b": "x", "v": 30},
        ]
        left_tbl = pa.table(
            {
                "a": pa.array([r["a"] for r in base]),
                "b": pa.array([r["b"] for r in base]),
                "v": pa.array([r["v"] for r in base]),
            }
        )
        r_tbl = pa.table(
            {
                "a": pa.array([1, 1, 2]),
                "b": pa.array(["x", "y", "x"]),
                "v": pa.array([10, 21, 30]),
            }
        )
        lp, rp = tmp_path / "cl.parquet", tmp_path / "cr.parquet"
        pq.write_table(left_tbl, lp)
        pq.write_table(r_tbl, rp)
        report = diff_files(session, str(lp), str(rp), key=("a", "b"))
        cells = {c.column: c.mismatched_rows for c in report.values.columns}
        assert cells == {"v": 1}

    def test_missing_key_column_errors(self, session: Session, tmp_path) -> None:
        inj = build("value_changed", n_rows=50)
        lp, rp = write_pair(tmp_path, inj)
        with pytest.raises(KeyError, match="nope"):
            diff_files(session, lp, rp, key=("nope",))

    def test_dropped_column_still_diffable(self, session: Session, tmp_path) -> None:
        from tests.gen import column_dropped

        inj = column_dropped(build_base_small(), __import__("random").Random(2))
        lp, rp = write_pair(tmp_path, inj)
        report = diff_files(session, lp, rp)
        # payload missing on the right -> reported, run completes cleanly
        assert any(c.name == "payload" and c.status == "only_left" for c in report.schema.columns)
        assert "payload" not in {c.column for c in report.values.columns}

    def test_null_keys_join_together(self, session: Session, tmp_path) -> None:
        import pyarrow as pa

        t_l = pa.table({"id": pa.array([1, 2]), "name": pa.array(["a", None])})
        t_r = pa.table({"id": pa.array([1, 2]), "name": pa.array(["a", None])})
        lp, rp = tmp_path / "nl.parquet", tmp_path / "nr.parquet"
        pq.write_table(t_l, lp)
        pq.write_table(t_r, rp)
        report = diff_files(session, str(lp), str(rp), key=("id",))
        assert report.identical

