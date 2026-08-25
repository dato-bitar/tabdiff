# BENCHMARKS

All numbers measured with the committed code, no cherry-picking. Reproduce
with `make bench` (or `uv run python benchmarks/run_benchmark.py`).

## Hardware

| | |
|---|---|
| CPU | AMD Ryzen AI 7 350, 16 logical cores |
| RAM | 13.8 GB |
| OS | Windows 11 (10.0.26200), Python 3.12.14 |
| Disk | NVMe SSD |

## Headline: 10M rows x 20 columns, Parquet vs Parquet

Files: ~915 MB each (zstd). Deviations injected into the right side:
5,000 rows deleted, 5,000 rows added, 9,994 cell values changed (+17.5 on a
float column; exact count computed from the injection masks).

| Scenario | Wall time | Verdict |
|---|---|---|
| `joindiff`, statistics disabled (`--no-stats`) | **49.7 s** | all deviations found exactly |
| default CLI run (json output + column statistics over all 20 columns) | **58.9 s** | all deviations found exactly |

Both are end-to-end times including opening and fully reading both parquet
files. The success criterion from the master prompt (<60 s) is met in both
configurations on this hardware.

Correctness during the benchmark run:

```
left_only=5000 (expected 5000)
right_only=5000 (expected 5000)
value diffs found: {'c_float64_a': 9994}   (expected 9994)
```

## Smaller runs (same machine)

| Rows | joindiff | notes |
|---|---|---|
| 200k x 20 cols | 2.7-4.9 s | includes DuckDB warm-up |

## hashdiff performance characteristics (measured, not guessed)

hashdiff exists for sources that cannot or should not be pulled into one
engine (two separate Postgres instances). Its portable checksum pipeline
(md5 over canonical fields + strpos-based hex arithmetic) costs roughly
20 us/row/scan in DuckDB on this machine:

- 2M rows x 20 cols, local parquet files, scattered changes:
  coarse scans ~41-50 s per side, pulls ~2 s each.

That is much slower than joindiff for local data - by design it does work
that joindiff gets "for free" from one engine. Consequence: for local files,
auto strategy always picks joindiff; hashdiff's value case is remote engines,
where the alternative is shipping whole tables over the wire. See LIMITS.md.

## Generation cost

Generating and writing the two 10M-row parquet files takes ~51 s
(numpy/pyarrow only, no pandas). Generation time is NOT part of the diff
numbers above.
