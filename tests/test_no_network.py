"""No-exfiltration guarantee: local diffs must never open a network socket.

The whole point of tabdiff is that data stays on the machine. This test
poisons Python's socket layer, runs a purely local diff end-to-end through
the CLI, and asserts it succeeds anyway. DuckDB reads Parquet/CSV via file
IO (not Python sockets), so any attempt to phone home would explode here.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import patch

import pyarrow.parquet as pq
from typer.testing import CliRunner

from tabdiff.cli import app
from tests.gen import build


class NetworkIsBlocked(AssertionError):
    pass


def _poison_socket() -> Any:
    original = socket.socket.connect

    def guard(self: Any, address: Any) -> Any:
        raise NetworkIsBlocked(f"tabdiff tried to open a connection to {address!r}")

    return patch.object(socket.socket, "connect", guard), original


def test_local_diff_never_touches_network(tmp_path: Any) -> None:
    inj = build("value_changed", n_rows=120, seed=99)
    p1, p2 = tmp_path / "a.parquet", tmp_path / "b.parquet"
    pq.write_table(inj.left, p1)
    pq.write_table(inj.right, p2)

    patcher, _orig = _poison_socket()
    with patcher:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "diff",
                str(p1),
                str(p2),
                "--key",
                "id",
                "--format",
                "json",
                "--no-stats",  # stats are local too; keep the run minimal
            ],
        )
    assert result.exit_code == 1  # differences found - the run itself succeeded


def test_identical_local_diff_never_touches_network(tmp_path: Any) -> None:
    same = build("order_shuffled", n_rows=100, seed=100)
    p1, p2 = tmp_path / "c.parquet", tmp_path / "d.parquet"
    pq.write_table(same.left, p1)
    pq.write_table(same.right, p2)

    patcher, _orig = _poison_socket()
    with patcher:
        runner = CliRunner()
        result = runner.invoke(app, ["diff", str(p1), str(p2), "--key", "id"])
    assert result.exit_code == 0


def test_hashdiff_local_never_touches_network(tmp_path: Any) -> None:
    diffed = build("row_added", n_rows=150, seed=101)
    p1, p2 = tmp_path / "e.parquet", tmp_path / "f.parquet"
    pq.write_table(diffed.left, p1)
    pq.write_table(diffed.right, p2)

    patcher, _orig = _poison_socket()
    with patcher:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["diff", str(p1), str(p2), "--key", "id", "--strategy", "hash", "-f", "json"],
        )
    assert result.exit_code == 1
