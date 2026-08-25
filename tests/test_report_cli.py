"""M8: output formats (json/markdown/rich), exit codes via the CLI."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from tabdiff.cli import app
from tabdiff.diff import RunOptions, run_diff
from tabdiff.report import to_json, to_markdown
from tabdiff.session import Session
from tabdiff.source import bind_source
from tabdiff.strategy.join_diff import run_join_diff
from tests.gen import build


@pytest.fixture()
def session() -> Session:
    s = Session()
    yield s
    s.close()


def _pair(tmp_path: Any, kind: str, n: int, seed: int) -> tuple[str, str]:
    inj = build(kind, n_rows=n, seed=seed)
    lp, rp = tmp_path / "l.parquet", tmp_path / "r.parquet"
    import pyarrow.parquet as pq

    pq.write_table(inj.left, lp)
    pq.write_table(inj.right, rp)
    return str(lp), str(rp)


class TestJsonOutput:
    def test_schema_version_present_and_stable(self, session: Session, tmp_path: Any) -> None:
        lp, rp = _pair(tmp_path, "value_changed", 100, 3)
        report = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        doc = json.loads(to_json(report))
        assert doc["schema_version"] == 1
        assert set(doc) >= {"schema_version", "identical", "meta", "schema", "counts", "values"}
        assert doc["identical"] is False
        assert doc["meta"]["strategy"] == "join"
        assert doc["meta"]["key"] == ["id"]

    def test_json_is_deterministic(self, session: Session, tmp_path: Any) -> None:
        lp, rp = _pair(tmp_path, "value_changed", 100, 3)
        r1 = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        r2 = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        d1, d2 = json.loads(to_json(r1)), json.loads(to_json(r2))
        # strip timing, which legitimately varies
        d1["meta"].pop("duration_s")
        d2["meta"].pop("duration_s")
        assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)

    def test_identical_run_json(self, session: Session, tmp_path: Any) -> None:
        lp, rp = _pair(tmp_path, "order_shuffled", 60, 4)
        report = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        doc = json.loads(to_json(report))
        assert doc["identical"] is True

    def test_examples_carry_key_and_values(self, session: Session, tmp_path: Any) -> None:
        lp, rp = _pair(tmp_path, "value_changed", 80, 5)
        report = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        doc = json.loads(to_json(report))
        col = doc["values"]["columns"][0]
        assert col["column"] == "score"
        ex = col["examples"][0]
        assert set(ex) == {"key", "left", "right"}


class TestMarkdownOutput:
    def test_sections_present(self, session: Session, tmp_path: Any) -> None:
        lp, rp = _pair(tmp_path, "value_changed", 100, 6)
        report = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        md = to_markdown(report)
        assert "# tabdiff result" in md
        assert "DIFFERENCES FOUND" in md
        for section in (
            "## Schema",
            "## Row counts",
            "## Value differences",
            "## Column statistics",
        ):
            assert section in md, section

    def test_identical_verdict(self, session: Session, tmp_path: Any) -> None:
        lp, rp = _pair(tmp_path, "order_shuffled", 50, 7)
        report = run_diff(lp, rp, RunOptions(key=("id",)), session=session)
        assert "IDENTICAL" in to_markdown(report)


class TestCli:
    runner = CliRunner()

    def test_exit_codes(self, tmp_path: Any) -> None:
        same = build("order_shuffled", n_rows=60, seed=9)
        diffed = build("value_changed", n_rows=60, seed=9)
        import pyarrow.parquet as pq

        p1 = tmp_path / "a.parquet"
        p2 = tmp_path / "b.parquet"
        p3 = tmp_path / "c.parquet"
        pq.write_table(same.left, p1)
        pq.write_table(same.right, p2)
        pq.write_table(diffed.right, p3)

        r_same = self.runner.invoke(app, ["diff", str(p1), str(p2), "--key", "id"])
        assert r_same.exit_code == 0, r_same.output

        r_diff = self.runner.invoke(app, ["diff", str(p1), str(p3), "--key", "id"])
        assert r_diff.exit_code == 1, r_diff.output

    def test_exit_code_2_on_bad_source(self, tmp_path: Any) -> None:
        r = self.runner.invoke(app, ["diff", "does_not_exist.parquet", "also_missing.csv"])
        assert r.exit_code == 2

    def test_exit_code_2_on_duplicate_key(self, tmp_path: Any) -> None:
        inj = build("duplicate_key_introduced", n_rows=40)
        import pyarrow.parquet as pq

        p1, p2 = tmp_path / "d1.parquet", tmp_path / "d2.parquet"
        pq.write_table(inj.left, p1)
        pq.write_table(inj.right, p2)
        r = self.runner.invoke(app, ["diff", str(p1), str(p2), "--key", "id"])
        assert r.exit_code == 2

    def test_json_flag_output(self, tmp_path: Any) -> None:
        diffed = build("value_changed", n_rows=50, seed=11)
        import pyarrow.parquet as pq

        p1, p2 = tmp_path / "j1.parquet", tmp_path / "j2.parquet"
        pq.write_table(diffed.left, p1)
        pq.write_table(diffed.right, p2)
        r = self.runner.invoke(app, ["diff", str(p1), str(p2), "--key", "id", "--format", "json"])
        assert r.exit_code == 1
        doc = json.loads(r.output)
        assert doc["schema_version"] == 1
        assert doc["values"]["changed_rows"] > 0

    def test_markdown_to_file(self, tmp_path: Any) -> None:
        diffed = build("value_changed", n_rows=40, seed=12)
        import pyarrow.parquet as pq

        p1, p2 = tmp_path / "m1.parquet", tmp_path / "m2.parquet"
        pq.write_table(diffed.left, p1)
        pq.write_table(diffed.right, p2)
        out = tmp_path / "report.md"
        r = self.runner.invoke(
            app,
            ["diff", str(p1), str(p2), "--key", "id", "-f", "markdown", "-o", str(out)],
        )
        assert r.exit_code == 1
        text = out.read_text(encoding="utf-8")
        assert "# tabdiff result" in text

    def test_tolerance_flag_changes_verdict(self, tmp_path: Any) -> None:
        diffed = build("value_changed", n_rows=60, seed=13)
        import pyarrow.parquet as pq

        p1, p2 = tmp_path / "t1.parquet", tmp_path / "t2.parquet"
        pq.write_table(diffed.left, p1)
        pq.write_table(diffed.right, p2)
        strict = self.runner.invoke(app, ["diff", str(p1), str(p2), "--key", "id"])
        loose = self.runner.invoke(
            app,
            [
                "diff",
                str(p1),
                str(p2),
                "--key",
                "id",
                "--tolerance-abs",
                "1000000",
                "--tolerance-rel",
                "10",
            ],
        )
        assert strict.exit_code == 1
        assert loose.exit_code == 0

    def test_strategy_override_hash_runs_locally(self, tmp_path: Any) -> None:
        diffed = build("value_changed", n_rows=80, seed=14)
        import pyarrow.parquet as pq

        p1, p2 = tmp_path / "h1.parquet", tmp_path / "h2.parquet"
        pq.write_table(diffed.left, p1)
        pq.write_table(diffed.right, p2)
        r = self.runner.invoke(
            app, ["diff", str(p1), str(p2), "--key", "id", "--strategy", "hash", "-f", "json"]
        )
        doc = json.loads(r.output)
        assert doc["meta"]["strategy"] == "hash"
        assert doc["identical"] is False

    def test_guess_key_announced_not_silent(self, tmp_path: Any) -> None:
        t = build("value_changed", n_rows=30, seed=15)
        import pyarrow.parquet as pq

        p1, p2 = tmp_path / "g1.parquet", tmp_path / "g2.parquet"
        pq.write_table(t.left, p1)
        pq.write_table(t.right, p2)
        s = Session()
        try:
            src_l = bind_source(s, "l", str(p1))
            src_r = bind_source(s, "r", str(p2))
            from tabdiff.keycheck import guess_key

            key = guess_key([c.name for c in src_l.columns()], [c.name for c in src_r.columns()])
            assert key == ["id"]
        finally:
            s.close()
        # and the report records it
        report = run_diff(str(p1), str(p2), RunOptions(), session=None)
        assert report.meta.key == ["id"]

    def test_full_flag_includes_more_examples(self, session: Session, tmp_path: Any) -> None:
        inj = build("value_changed", n_rows=200, seed=16)
        import pyarrow.parquet as pq

        p1, p2 = tmp_path / "f1.parquet", tmp_path / "f2.parquet"
        pq.write_table(inj.left, p1)
        pq.write_table(inj.right, p2)
        bounded = run_join_diff(
            session,
            bind_source(session, "l", str(p1)),
            bind_source(session, "r", str(p2)),
            key_cols=["id"],
            opts=__import__("tabdiff").canon.CompareOptions(),
            examples_n=5,
        )
        full = run_join_diff(
            session,
            bind_source(session, "fl", str(p1)),
            bind_source(session, "fr", str(p2)),
            key_cols=["id"],
            opts=__import__("tabdiff").canon.CompareOptions(),
            full=True,
        )
        n_bounded = len(bounded.values.columns[0].examples)
        n_full = len(full.values.columns[0].examples)
        assert n_bounded <= 5 < min(n_full, inj.expected.cells["score"])
