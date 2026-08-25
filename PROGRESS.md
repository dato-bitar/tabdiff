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
| R | Release prep: CI attempt, pushdown fix, dirty schemas, cut | - | done |

Final suite: **281 tests green** (`make check`-equivalent), of which
4 hypothesis property tests and one test per injection type per strategy.
Postgres integration runs against a real testcontainer on this machine;
the 12 postgres-marker tests run green explicitly.

## Headline result

`tabdiff diff left.parquet right.parquet --key id` on **10M rows x 20 cols**:
**49.7 s** without statistics, **58.9 s** with full column statistics —
both under the 60 s target; every injected deviation found exactly.
Details: BENCHMARKS.md. Release smoke (`--rows 200000`): PASS, 3.9 s,
all ground-truth counts exact.

## Release prep (R) - what happened

### A1 - CI could NOT be run (blocker)

`.github/workflows/ci.yml` has still never executed on GitHub Actions.
The `gh` CLI is installed but BOTH credentials are invalid (verified with
`gh api user` → HTTP 401): the `GITHUB_TOKEN` environment variable and the
token stored in gh's own config. Interactive `gh auth login` was not an
option (autonomous run). Per instructions A1 was skipped after documenting
this blocker; no remote repository exists yet.

Done locally instead: `.gitattributes` (`* text=auto eol=lf`) added and the
index renormalized (no changes needed - files were already LF). The README
badge was deliberately NOT added because there is no green CI run to point
to. First person with valid `gh auth login` should: create the private repo,
push main + tag `v0.1.0b1`, watch the run, fix CRLF/uv/service issues if any
appear (the workflow file itself is untested).

### A2 - Postgres pushdown was dead code; now fixed AND verified active

The pushdown path had NEVER worked anywhere: feature detection always failed
and every run silently used the local-scan fallback (DECISIONS §31). Root
causes found by measurement, all fixed (DECISIONS §35-37):
alias-vs-URL form required by current `postgres_scanner`, remote relation
must use postgres names not the attach alias, `ends_with()` does not exist
in Postgres, int4 overflow in portable hex arithmetic, duplicate aggregate
column names rejected by `postgres_query()`, and an Arrow dict-as-tuple
decoding bug in the never-executed path. `DiffMeta.execution_path` now makes
the chosen path visible per side in JSON/markdown/rich. Integration tests
assert zero-diff parity with pushdown ACTIVE and with FORCED fallback, plus
injection detection on both paths. LIMITS.md states precisely where pushdown
is verified to work (DuckDB 1.5.5, postgres_scanner 41223e5, PG 16, this
machine) and what degrades otherwise.

### A3 - five dirty-schema injections added (18 total)

`User ID` vs `user_id`, special characters in names, case-colliding column
pairs, ~10 KB cells, ~99% NULL columns - each with dedicated assertions on
BOTH strategies (joindiff + hashdiff). Nothing crashed; nothing miscounted.
One honest finding documented in LIMITS.md: DuckDB renames case-colliding
duplicates (`Delta`+`delta` → `Delta`+`delta_1`) identically on both sides,
so reports show effective names.

### A4/A5 - release cut

Version 0.1.0b1 (pyproject + `__init__`), CHANGELOG.md (Keep-a-Changelog)
distilled from this file, git tag `v0.1.0b1` (set once more on the FINAL
commit after the last fixes - no remote ever existed, so nothing was
rewritten), full check sequence green, benchmark smoke green. One extra
release-blocker found and fixed while verifying: `tabdiff --version`
exited 2 instead of printing the version (typer callback needed
`invoke_without_command=True`); regression test added.

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
- All 18 injection types have dedicated passing tests against BOTH strategies.
- Self-diff property (any generated table vs itself → zero diffs) covered by
  hypothesis properties plus explicit shuffled-order tests per strategy.
- Cross-engine parity: identical PG table vs parquet export → zero diffs via
  hashdiff, asserted on BOTH execution paths (pushdown and local-scan; the
  classic data-diff failure mode, explicitly tested).
- Exit codes 0/1/2 verified through the typer CLI test runner.
- No-exfiltration: three tests poison Python's socket layer and require
  successful local diffs (join + hash) anyway.
