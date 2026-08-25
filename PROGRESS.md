# PROGRESS

Status log, updated after every milestone. Newest at the bottom.
Time budget from the master prompt: ~10 h across M0–M9.

| # | Milestone | Budget | Status |
|---|-----------|--------|--------|
| M0 | Scaffold: uv, ruff, mypy --strict, CI, Makefile, MIT | 0:30 | done |
| M1 | Source abstraction + Parquet/CSV/DuckDB adapters | 1:00 | done |
| M2 | Schema diff + type normalization + ≥30 tests | 1:15 | done |
| M3 | Test-data generator, 13 injection types | 1:00 | done |
| M4 | joindiff over DuckDB, all 13 injections detected | 1:30 | done |
| M5 | Postgres adapter + roundtrip test | 1:00 | done |
| M6 | hashdiff with checksum bisecting | 1:30 | done |
| M7 | Column-statistics drift | 0:30 | done |
| M8 | rich/json/markdown output + exit codes | 0:45 | done |
| M9 | 10M benchmark, README, LIMITS, cleanup | 1:00 | done |

## Deviation notes

- `make` is not installed on the dev machine (Windows). Checks were executed
  as the equivalent command sequence documented in DECISIONS.md §1; the
  Makefile targets exist and are exercised by CI.
- uv 0.12.5 and CPython 3.12.14 were installed during M0 because the machine
  had neither.

## Not implemented

(deliberate non-goals and anything cut, kept honest here)

- Snowflake/BigQuery/Redshift adapters: trait/protocol prepared, not built
  (per spec).
- dbt integration: postponed (per spec).
- Web UI / cloud components: non-goal (per spec).

## Verification status

- `make check` (ruff format+check, mypy --strict, pytest): green at every commit.
- Injection-type coverage: all 13 generator injections each have a dedicated
  passing test against joindiff and hashdiff.
