# DECISIONS

Every decision this project made that the master prompt did not prescribe, in
chronological order. Newest at the bottom.

## M0 — scaffold

1. **`make` is not available on the primary dev machine (Windows).**
   The `Makefile` exists (spec requirement, used on Linux/CI), but all checks
   are run locally through the equivalent direct commands:
   `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest`.
   Documented in PROGRESS.md; CI runs `make`-equivalent steps explicitly.
2. **uv installed on demand** (0.12.5) together with CPython 3.12.14 — the
   machine only had 3.11.
3. **Docker IS available** on this machine (several postgres:16 containers are
   running), so testcontainers-based Postgres tests will be implemented for
   real, not skipped. A `TABDIFF_TEST_PG_DSN` env var bypasses testcontainers
   (used by CI service container).
4. **DuckDB type strings as the internal lingua franca.** Every source adapter
   (Parquet, CSV, DuckDB file, Postgres) reports its schema *through DuckDB*
   (`DESCRIBE SELECT ...`), so schema diffing and normalization deal with one
   type system instead of N. Postgres types arrive pre-mapped by the
   `postgres_scanner` extension.
5. **One canonical-text function per engine is the single source of truth for
   value equality.** Two values are equal iff their canonical texts are equal
   (except tolerance-compared numerics). The same canonical text feeds the
   hashdiff row hashes. There is a Python reference implementation of the
   canonicalizer; property tests assert DuckDB SQL output == Python reference,
   and live Postgres tests assert Postgres SQL output == Python reference.
   This is the mechanism that keeps joindiff and hashdiff semantically
   identical and keeps cross-engine checksums comparable.
6. **No pandas anywhere**, including report rendering (spec: "lieber nicht" —
   we chose "not at all").
7. **JSON semantic comparison in two stages:** a fast SQL-level stage
   (`json_serialize`) catches whitespace/formatting differences inside the
   engine; candidate mismatches are re-checked in Python with a recursive,
   key-sorted canonicalization so key-order-only differences are correctly
   reported as equal. Cost is proportional to the number of JSON mismatches,
   which is rare.
8. **Postgres pushdown strategy for hashdiff:** prefer executing the checksum
   aggregate inside Postgres via the `postgres_query()` table function of
   `postgres_scanner`; if unavailable in the installed extension version,
   fall back to scanning the attached table through DuckDB (correct, slower).
   Feature-detect at runtime, document which path was taken.
9. **hashdiff bucketing over md5(key) hex prefixes** rather than raw key-range
   partitioning: works uniformly for any key type (strings, composites, UUIDs),
   needs no index, and both engines have identical md5 semantics. Buckets are
   text ranges of fixed-width hex prefixes; lexicographic compare == numeric
   compare.
10. **Row-hash aggregation must be transposition-safe:** plain `sum(hash)`
    cannot see two rows swapping values within a segment. We combine three
    order-insensitive aggregates: `sum(h1)`, `sum(h2)` (two independent
    60-bit hashes derived from md5 hex digits via portable arithmetic), and
    `sum((h1 mod p) * (h2 mod p) mod p)` with prime p < 2^31, which detects
    transpositions and stays inside signed 64-bit range in both engines.
11. **Exit-code mapping:** any `TabDiffError` → exit 2; differences found →
    exit 1; success → 0. Unexpected Python exceptions also exit 2 after
    printing a traceback (a crash is an error, not a difference).
12. **CSV reading policy:** DuckDB CSV sniffer decides types; the user can
    force `--all-varchar` to defer typing to the normalization layer. Boolean
    aliases (`t/yes/y/1`) and empty-as-null handling happen in normalization,
    not at read time, so both sides go through identical rules.

## M1 — sources

13. **Source spec grammar:** bare filesystem paths (`*.parquet`, `*.csv`),
    `duckdb://<path>/<table>`, `postgres://` / `postgresql://` URLs with an
    optional `/schema.table` suffix. Key=value Postgres conninfo strings are
    deliberately NOT accepted on the CLI (ambiguous against filenames);
    URLs only. Documented in README.
14. **BoundSource abstraction:** binding registers a view/attach into the
    shared DuckDB session under an alias and exposes `relation_sql()`,
    typed column metadata, row count, and an escape hatch to run SQL against
    the source's own engine (needed by hashdiff pushdown).
15. **Extension policy:** DuckDB extensions (`postgres`) are installed once,
    eagerly, when a Postgres source is first bound — never implicitly for
    local formats. `autoinstall` stays enabled so a fresh machine works out
    of the box, but local Parquet/CSV/DuckDB diffs never touch the network
    (verified by a dedicated no-network test).

## M2 — types & schema

16. **Canonical type lattice:** {boolean, integer, decimal(p,s), float,
    string, binary, date, time, timestamp(naive|tz), json, uuid, other}.
    Comparability matrix decides whether value-diff runs for a column pair;
    every mismatch always appears in the schema diff regardless.
17. **Timestamp precision:** default mode `coarse` rounds/truncates both
    sides to the coarser of the two precisions and records an assumption in
    the report. `--ts-precision={us,ms,ns}` forces an explicit unit instead.
18. **Naive vs tz timestamps:** schema diff flags it loudly; value comparison
    only proceeds if `--assume-tz TZ` is given (naive interpreted in TZ, then
    compared in UTC). The assumption is printed in every output format.
19. **DECIMAL vs FLOAT never silently compares as equal types:** schema diff
    reports a lossy-widening issue even when values coincide.
20. **Unicode:** NFC normalization applied to all string comparisons and
    canonical texts via DuckDB's `nfc_normalize`. Collation ignored.
21. **Booleans:** accepted aliases normalized to real booleans:
    `true/t/yes/y/1` / `false/f/no/n/0` (case-insensitive), applied wherever a
    string-typed side meets a boolean-typed side.

## M4/M6 — strategies

22. **joindiff is one SQL statement per concern** (counts, per-column mismatch
    counts, examples) over a FULL OUTER JOIN on the canonicalized key, never
    per-row Python work. Example extraction uses `LIMIT n` queries per
    affected column only.
23. **key-less mode** compares multisets of whole-row hashes; the report says
    explicitly that only "row missing/new" statements are possible, never
    cell-level attribution.
24. **hashdiff leaf resolution** pulls only differing leaves into a local
    DuckDB temp-table join, then reuses exactly the same comparison machinery
    as joindiff — one code path for final verdicts, so hashdiff cannot drift
    from joindiff semantics.

## M8 — reporting

25. **JSON output is versioned (`schema_version: 1`) and additive-only:**
    new fields may appear, existing fields never change meaning or shape.
    Snapshot-tested.
26. **Rich rendering degrades gracefully** when stdout is not a TTY (CI):
    plain tables without ANSI codes.

## M4–M9 — strategies, reporting, scale

27. **Parquet time units are re-overlaid from the Arrow schema:** DuckDB
    normalizes every parquet timestamp to microseconds on read, silently
    destroying the s/ms/ns information that precision-aware comparison needs.
    `ParquetSource.columns()` therefore overrides types using the file's
    Arrow schema; nullability likewise comes from Arrow rather than DuckDB's
    blanket YES.
28. **Schema-diff severity model:** only *missing columns* block a value diff
    (`has_blocking`). Per-column incompatibilities warn loudly and skip just
    that column; benign representational notes (timestamp units, decimal
    scale) appear in the report but do NOT flip the exit code - "identical"
    means no semantic difference, and CI users should not fail on units.
29. **Float canonicalization is pinned by an idempotence property, not
    byte-parity with Python:** probing showed DuckDB's double-to-text allows
    ~1 ULP slop, so exact mirroring is guesswork. The property test asserts
    whatever DuckDB prints re-parses to a value with identical canonical
    text (transitivity of equality). Granularity documented in LIMITS.md;
    cross-engine floats recommend tolerance flags.
30. **typer 0.27 quirk:** an option callback with `is_eager=True` fires even
    when the flag is absent, which made every CLI invocation print the
    version. Workaround: check the flag inside the callback body (the pattern
    FastAPI docs use) instead of passing `callback=`.
31. **hashdiff pushdown degradation:** if the installed postgres extension
    lacks `postgres_query()`, the postgres side renders its checksum SQL in
    DUCKDB dialect against the attached table (correct, moves data). Dialect
    selection happens once per side via `preferred_engine()` so both sides
    always render consistently within a phase. Recorded as a report warning.
32. **hashdiff performance accepted, not hidden:** measured ~20 us/row/scan
    for the portable pipeline locally. A temp-table materialization cache was
    considered and consciously deferred (complexity vs benefit for the local
    case, where joindiff wins anyway). Numbers in BENCHMARKS.md, caveat in
    LIMITS.md.
33. **JSON column detection is opt-in per name** (`--json-columns`) because
    parquet/CSV store JSON as plain text with no reliable logical type. The
    two-stage compare (SQL fast path + bounded Python refinement) keeps
    counts honest: beyond 10k candidates per column counts are labelled
    approximated rather than silently wrong.
34. **Benchmark ground truth computes overlap exactly:** injected value
    changes that coincide with deleted rows are subtracted from expectations
    via set arithmetic on the injection masks - the generator never guesses.
