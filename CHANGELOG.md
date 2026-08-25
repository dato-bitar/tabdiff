# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0b1] - 2026-08-25

First public beta. All core milestones (M0–M9) complete; 280 tests green
(ruff format+check, mypy --strict, pytest including live-Postgres tests).

### Added

- Sources: Parquet, CSV, DuckDB database files, and Postgres tables via
  DuckDB's `postgres_scanner` extension - all bound into one embedded
  DuckDB session.
- Two diff strategies with automatic selection:
  - `joindiff`: one FULL OUTER JOIN on the canonicalized key, all
    comparisons in SQL; exact, single pass.
  - `hashdiff`: hierarchical checksum bisecting for sources in different
    engines; matching segments discarded, differing leaves pulled and
    re-checked by the exact joindiff machinery.
- True Postgres pushdown for hashdiff checksums via `postgres_query()`;
  per-side execution path (`pushdown` vs `local-scan`) reported in every
  output format (`meta.execution_path`) and clean detect-time fallback.
- Canonical-text equality engine with per-engine SQL (DuckDB + Postgres)
  and a property-tested Python reference: NFC-normalized strings,
  boolean alias normalization, decimal trimming, float shortest-repr
  smoothing, timestamp precision handling (coarse/s/ms/us/ns),
  naive-vs-tz gating via `--assume-tz`, JSON fast-path + bounded Python
  semantic refinement.
- Schema diff with a typed verdict lattice (same/benign/widening/lossy/
  needs_tz/string_cast/bool_alias/incompatible/only_left/only_right);
  only missing columns block, representation notes never fail CI.
- Row-count section, cell-level value differences with bounded examples,
  per-column statistics drift, key-less mode (row multiset comparison).
- Output formats: rich terminal (TTY-aware), versioned JSON
  (`schema_version: 1`, additive-only), markdown to file or stdout;
  exit codes 0 = identical, 1 = differences, 2 = error.
- CLI flags: `--strategy`, `--key`/key guessing announced in the report,
  `--tolerance-abs/--tolerance-rel`, `--assume-tz`,
  `--ts-precision {coarse,s,ms,us,ns}`, `--all-varchar`,
  `--json-columns`, `--treat-empty-as-null`, `--examples N`, `--full`,
  `--leaf-rows`, per-section switches.
- Synthetic ground-truth generator with 18 injection types, each pinned
  by dedicated tests on BOTH strategies, including dirty-schema cases:
  case/space column-name mismatches, special characters in names,
  case-colliding column pairs, ~10 KB cell payloads, ~99% NULL columns.
- No-exfiltration guarantee for local formats, enforced by tests that
  poison Python's socket layer.
- Benchmarks: 10M rows x 20 cols end-to-end in 49.7 s (58.9 s with full
  statistics), under the 60 s target; details in BENCHMARKS.md.

### Fixed

- Postgres pushdown had never actually executed (silent fallback on every
  machine): fixed alias-vs-URL form for newer `postgres_scanner`, remote
  relation naming, a Postgres-only missing function in float
  canonicalization, int4 overflow in portable hex arithmetic, duplicate
  result-column names, and an Arrow decoding bug in the never-run path.
  Now verified active against live Postgres with integration tests that
  assert which path ran.

### Known limitations

See LIMITS.md - notably ~15-significant-digit float equality granularity,
unreliable cross-engine hashdiff for JSON columns, and DuckDB renaming of
case-colliding column names on read.
