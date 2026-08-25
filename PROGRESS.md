# PROGRESS

Status log, updated after every milestone. Time budget from the master
prompt: ~10 h across M0–M9.

| # | Milestone | Budget | Status |
|---|-----------|--------|--------|
| M0 | Scaffold: uv, ruff, mypy --strict, CI, Makefile, MIT | 0:30 | done |
| M1 | Source abstraction + Parquet/CSV/DuckDB adapters | 1:00 | done |
| M2 | Schema diff + type normalization + ≥30 tests | 1:15 | done (85 new tests) |
| M3 | Test-data generator, 13 injection types | 1:00 | done |
| M4 | joindiff over DuckDB, all 13 injections detected | 1:30 | done |
| M5 | Postgres adapter + roundtrip test | 1:00 | done (testcontainers + live) |
| M6 | hashdiff with checksum bisecting | 1:30 | done incl. cross-engine parity |
| M7 | Column-statistics drift | 0:30 | done (+ keyless mode) |
| M8 | rich/json/markdown output + exit codes | 0:45 | done |
| M9 | 10M benchmark, README, LIMITS, cleanup | 1:00 | done |

Final suite: **241 tests green** (`make check`-equivalent), of which
4 hypothesis property tests and one test per injection type per strategy.
Postgres integration runs against a real testcontainer on this machine.

## Headline result

`tabdiff diff left.parquet right.parquet --key id` on **10M rows x 20 cols**:
**49.7 s** without statistics, **58.9 s** with full column statistics —
both under the 60 s target; every injected deviation found exactly.
Details: BENCHMARKS.md.

## Deviation notes

- `make` is not installed on the dev machine (Windows). Checks were executed
  as the equivalent command sequence documented in DECISIONS.md §1; the
  Makefile targets exist and are exercised by CI.
- uv 0.12.5 and CPython 3.12.14 were installed during M0 because the machine
  had neither.
- The dev machine HAS Docker with postgres:16 images, so Postgres tests run
  for real via testcontainers (no fallback needed).
- DuckDB's ICU extension works for named time zones but needs `pytz` for
  client-side TIMESTAMPTZ conversion; pytz was added as a runtime dependency
  (tiny, no security surface).

## Not implemented

(deliberate non-goals and anything cut, kept honest here)

- Snowflake/BigQuery/Redshift adapters: source abstraction prepared
  (`BoundSource.preferred_engine()`), nothing built (per spec).
- dbt integration: postponed (per spec).
- Web UI / cloud components / telemetry: non-goal (per spec).
- hashdiff temp-table materialization cache: deferred; local hashdiff is
  measurably slower than joindiff and joindiff always wins there anyway.
  Measured numbers and rationale in BENCHMARKS.md/LIMITS.md.
- Cross-engine hashdiff for JSON columns: documented unreliable (key-order
  canonicalization differs between jsonb and DuckDB text). joindiff handles
  it correctly via Python refinement.
- `--full` streaming to disk for gigantic diffs: capped at 200k cells/column,
  documented in LIMITS.md.

## Verification status

- Full check green at every commit (ruff format+check, mypy --strict, pytest).
- All 13 injection types have dedicated passing tests against BOTH strategies.
- Self-diff property (any generated table vs itself → zero diffs) covered by
  hypothesis properties plus explicit shuffled-order tests per strategy.
- Cross-engine parity: identical PG table vs parquet export → zero diffs via
  hashdiff (the classic data-diff failure mode, explicitly tested).
- Exit codes 0/1/2 verified through the typer CLI test runner.
- No-exfiltration: three tests poison Python's socket layer and require
  successful local diffs (join + hash) anyway.
