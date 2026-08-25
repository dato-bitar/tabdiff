# LIMITS.md — where tabdiff gives wrong or useless results

This file is deliberately harsh. Everything below is real, verified behaviour
of this codebase as committed. If your use case hits one of these lines,
tabdiff is not (yet) the right tool for that job.

## Float comparison granularity

- DuckDB formats DOUBLE values with ~1 ULP tolerance when converting to text.
  Two doubles that differ by 1 ULP may therefore compare **equal**. Equality
  of float columns has a granularity of roughly 15 significant decimal
  digits, not bitwise equality.
- Across engines (DuckDB vs Postgres), shortest-representation formatting
  differs in edge cases (`1e+20` vs `1e20`, `-0`, exponent digits). Exact
  cross-engine float equality is best-effort. If you diff float columns
  between engines, pass `--tolerance-rel` explicitly; that mode compares
  numerically and is well-defined.

## JSON columns

- Semantic JSON comparison happens in two stages: a fast SQL stage
  (whitespace-insensitive) and a Python refinement pass that also ignores key
  order. The Python refinement is capped at 10,000 candidate mismatches per
  column; beyond that the count is labelled `approximated` instead of being
  silently wrong - but it is then an approximation.
- In hashdiff row hashes, JSON participates via its SQL form only. A pure
  key-order difference can therefore flag a segment as differing (extra work,
  never a wrong verdict - the leaf pull re-checks semantically).
- Cross-engine (Postgres jsonb vs DuckDB text), hashdiff checksums treat
  key order differently on each side. joindiff handles this correctly;
  cross-engine hashdiff with JSON columns is unreliable.

## Strings

- Comparison is NFC-normalized by design: `"café"` composed vs decomposed are
  equal and produce NO difference. If byte-exact string identity matters to
  you, tabdiff will not report these as diffs.
- The NULL marker and internal separators are sentinel strings
  (`chr(1)`-wrapped). A value that literally starts with those control bytes
  could collide with a NULL in hashing contexts. Astronomically unlikely,
  not impossible.

## Timestamps

- Naive vs tz-aware timestamps are NOT compared without `--assume-tz`. With
  a *wrong* zone you get a *wrong* diff (every row shifted) - detectable, but
  only if you look at the volume.
- Fixed-offset zones like `+02` are rejected by DuckDB's ICU; only IANA names
  (`Europe/Berlin`) work.
- Truncation uses `date_trunc`, i.e. floors toward negative infinity. For
  pre-1970 timestamps this differs from truncation-toward-zero semantics some
  tools use.

## Key handling

- A key with duplicate rows aborts the run (exit 2). No fuzzy mode exists.
- Rows whose key components are NULL join together under one synthetic key.
  If both sides contain many distinct NULL-key rows they will pair up
  arbitrarily-but-deterministically; cell diffs among them are reported, but
  "which original row" is meaningless there.

## hashdiff

- Checksums are sums of md5-derived integers plus a transposition-detecting
  product term. This is not cryptographic: an adversarial dataset crafted to
  collide could hide changes. For sync-checking of ordinary data this is
  irrelevant; against adversaries use joindiff.
- Performance: the portable pipeline costs ~20 us/row/scan (measured, see
  BENCHMARKS.md). On local files joindiff is always faster; hashdiff pays off
  when data cannot be pulled (remote engines) or when tables are huge and
  nearly identical.
- The postgres pushdown path requires a `postgres_scanner` version whose
  `postgres_query()` table function takes the ATTACH ALIAS as its first
  argument (current extension versions do; older URL-based forms fail the
  feature detect and degrade to scanning the attached table through DuckDB -
  correct, but moves all rows over the wire once per scan phase). Which path
  a run actually took is reported per side in `meta.execution_path`
  (`pushdown` vs `local-scan`) in every output format. Verified active on
  the development machine (DuckDB 1.5.5, postgres_scanner 41223e5, Postgres
  16); if your extension predates alias-style `postgres_query()`, expect
  local-scan.

## Schema handling

- Columns with incompatible types (e.g. VARCHAR vs BLOB) are excluded from
  the value diff with a warning. tabdiff never guesses casts between
  unrelated families.
- CSV typing relies on DuckDB's sniffer over the whole file (`sample_size=-1`
  default here); pathological mixed-type CSVs can still surprise. Use
  `--all-varchar` plus explicit expectations if in doubt.

## Scale

- Verified: 10M rows x 20 cols parquet-vs-parquet <60s end-to-end on the
  benchmark machine. Nothing above that scale has been measured. Counts,
  examples and stats run single-passes; memory scales with the number of
  *differences*, not rows, except column statistics which hold one aggregate
  row per side.

## Deliberate simplifications

- Exit code treats benign schema notes (timestamp units, decimal scale) as
  non-differences; they appear in the report but don't fail CI. If you want
  representation drift to fail CI, parse the JSON output yourself today.
- `--full` example output is capped at 200,000 cells per column as a process
  guard, not advertised as infinite.
