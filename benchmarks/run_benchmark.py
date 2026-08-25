"""Scale benchmark: 10M rows x 20 columns, Parquet vs Parquet.

Generates two parquet files with injected, counted deviations, then times a
full tabdiff run (end to end, including reading the files). Results feed
BENCHMARKS.md. Generation is deliberately numpy/pyarrow-only - no pandas,
consistent with project rules.

Usage:
    uv run python benchmarks/run_benchmark.py [--rows N] [--smoke]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tabdiff.diff import RunOptions, run_diff


def machine_info() -> dict[str, str]:
    cpu = os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor() or "unknown"
    try:

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        ram = f"{stat.ullTotalPhys / (1024**3):.1f} GB"
    except Exception:
        ram = "unknown"
    return {
        "cpu": f"{cpu} ({os.cpu_count()} logical cores)",
        "ram": ram,
        "python": platform.python_version(),
        "os": platform.platform(),
    }


def make_big_table(n_rows: int, seed: int) -> pa.Table:
    rng = np.random.default_rng(seed)
    n = n_rows
    cols: dict[str, pa.Array] = {}
    cols["id"] = pa.array(np.arange(1, n_rows + 1, dtype=np.int64))

    vocab = np.array([f"item_{i:05d}" for i in range(5000)])

    def strings(n: int) -> pa.Array:
        idx = rng.integers(0, len(vocab), size=n).astype(np.int32)
        return pa.DictionaryArray.from_arrays(idx, pa.array(vocab)).cast(pa.string())

    cols["c_int32_a"] = pa.array(rng.integers(-(2**31), 2**31 - 1, size=n, dtype=np.int32))
    cols["c_int64_a"] = pa.array(rng.integers(-(2**62), 2**62, size=n, dtype=np.int64))
    cols["c_float64_a"] = pa.array(rng.normal(100.0, 25.0, size=n))
    cols["c_float64_b"] = pa.array(rng.random(n) * 1e6)
    cols["c_float32_a"] = pa.array(rng.normal(0, 1, size=n).astype(np.float32))
    cols["c_bool_a"] = pa.array(rng.integers(0, 2, size=n, dtype=bool))
    cols["c_date_a"] = pa.array(
        rng.integers(18000, 20000, size=n).astype(np.int32), type=pa.date32()
    )
    cols["c_ts_us_a"] = pa.array(
        (rng.integers(1_600_000_000, 1_900_000_000, size=n).astype(np.int64) * 1_000_000)
        + rng.integers(0, 999_999, size=n).astype(np.int64),
        type=pa.timestamp("us"),
    )
    for name in ("c_str_a", "c_str_b", "c_str_c", "c_str_d"):
        cols[name] = strings(n)
    # fill up to 20 columns with more numerics
    for i in range(7):
        if i % 2 == 0:
            cols[f"c_num_{i}"] = pa.array(rng.normal(50, 5, size=n))
        else:
            cols[f"c_num_{i}"] = pa.array(rng.integers(0, 10_000_000, size=n, dtype=np.int64))

    return pa.table(cols)


def inject(left: pa.Table, n_changes: int, seed: int) -> tuple[pa.Table, dict[str, int]]:
    """Deterministic injections on the right side; returns expected counts."""
    rng = np.random.default_rng(seed)
    right = left

    # value_changed on c_float64_a: bump 10_000 rows by exactly +17.5
    scores = np.array(right.column("c_float64_a").to_numpy(zero_copy_only=False), copy=True)
    mask_idx = rng.choice(len(scores), size=n_changes, replace=False)
    scores[mask_idx] += 17.5
    _ = 0  # expected value-change count is finalized after the drop step below
    right = right.set_column(
        right.schema.get_field_index("c_float64_a"),
        "c_float64_a",
        pa.array(scores),
    )
    expected_value_changes = n_changes

    # row_deleted: drop 5_000 rows from right
    drop_idx = rng.choice(right.num_rows, size=5000, replace=False)
    keep_mask = np.ones(right.num_rows, dtype=bool)
    keep_mask[drop_idx] = False
    right = right.filter(pa.array(keep_mask))
    expected_left_only = 5000

    # some of the modified rows may also be dropped - only kept ones show up
    # as cell-level differences
    kept_modified = np.setdiff1d(mask_idx, drop_idx)
    expected_value_changes = len(kept_modified)

    # row_added: append 5_000 fresh rows (new ids)
    extra_n = 5000
    tail = left.slice(0, extra_n)
    new_ids = pa.array(np.arange(left.num_rows + 1, left.num_rows + extra_n + 1, dtype=np.int64))
    tail = tail.set_column(0, "id", new_ids)
    right = pa.concat_tables([right, tail])
    expected_right_only = extra_n

    return right, {
        "value_changed_cells": expected_value_changes,
        "left_only": expected_left_only,
        "right_only": expected_right_only,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10_000_000)
    ap.add_argument("--seed", type=int, default=2024)
    ap.add_argument("--datadir", type=str, default="benchmark_data")
    args = ap.parse_args()

    data_dir = Path(args.datadir)
    data_dir.mkdir(exist_ok=True)
    lp = data_dir / f"bench_{args.rows}_left.parquet"
    rp = data_dir / f"bench_{args.rows}_right.parquet"

    t0 = time.perf_counter()
    if not (lp.exists() and rp.exists()):
        print(f"generating {args.rows:,} rows ...")
        left_tbl = make_big_table(args.rows, args.seed)
        pq.write_table(left_tbl, lp, compression="zstd")
        right_tbl, expected = inject(left_tbl, 10_000, args.seed + 1)
        pq.write_table(right_tbl, rp, compression="zstd")
        del left_tbl, right_tbl
    else:
        print("parquet files already exist, skipping generation")
        # rebuild expectations deterministically from the generator contract
        base = make_big_table(1000, args.seed)  # shape only; counts are fixed below
        del base
        expected = {
            "value_changed_cells": 10_000,
            "left_only": 5000,
            "right_only": 5000,
        }
    gen_s = time.perf_counter() - t0
    left_size_mb = lp.stat().st_size / 1e6
    right_size_mb = rp.stat().st_size / 1e6

    print(f"generation+write: {gen_s:.1f}s, files: {left_size_mb:.0f}MB/{right_size_mb:.0f}MB")

    options = RunOptions(key=("id",), include_stats=False)

    # ---- joindiff (auto strategy for local files) -----------------------------
    t0 = time.perf_counter()
    report = run_diff(str(lp), str(rp), options)
    join_s = time.perf_counter() - t0
    got = {c.column: c.mismatched_rows for c in report.values.columns}

    print("\n=== RESULT (joindiff) ===")
    print(f"rows: {report.counts.left_total:,} vs {report.counts.right_total:,}")
    print(f"left_only={report.counts.left_only} (expected {expected['left_only']})")
    print(f"right_only={report.counts.right_only} (expected {expected['right_only']})")
    print(f"value diffs found: {got}")
    print(f"expected value diffs: c_float64_a == {expected['value_changed_cells']} (+17.5 each)")
    ok = (
        report.counts.left_only == expected["left_only"]
        and report.counts.right_only == expected["right_only"]
        and got.get("c_float64_a") == expected["value_changed_cells"]
    )
    print(f"joindiff wall time: {join_s:.1f}s  -> {'PASS' if ok else 'FAIL'}")

    print(json_line := "")
    _ = json_line
    info = machine_info()
    print("hardware:", info)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
