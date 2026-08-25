# tabdiff

**Local-first, cell-level diff for tabular data.** Compare Parquet, CSV,
DuckDB files and Postgres tables row-by-row and cell-by-cell — entirely on
your machine. No cloud, no account, no data leaving the host.

```text
tabdiff - DIFFERENCES FOUND
strategy=join key=id time=0.06s
      Value differences (7 changed rows)
┌────────┬──────────────────┬────────────────┐
│ column │ mismatching rows │ examples shown │
├────────┼──────────────────┼────────────────┤
│ score  │                7 │              7 │
└────────┴──────────────────┴────────────────┘
  score[2:20] : '84.27067901099386' -> '101.77067901099386'
  score[2:50] : '81.01574396360743' -> '98.51574396360743'
  ...
```

## The problem

Datafolds `data-diff` solved this problem well — but the open-source repo is
stale while development moved into the commercial cloud product. If you want
to diff a parquet file against a Postgres table *locally* today, there is no
maintained option.

## The solution

`tabdiff` runs everything through an embedded DuckDB engine:

- **Parquet / CSV** are read natively by DuckDB.
- **DuckDB database files** are attached.
- **Postgres** is attached via DuckDB's `postgres_scanner` extension; for
  large remote tables checksums are pushed down and computed *inside*
  Postgres (see `--strategy hash`).

Two strategies, chosen automatically (`--strategy {auto,join,hash}`):

1. **joindiff** — both sides reachable in one engine: one FULL OUTER JOIN on
   the key, all comparisons in SQL. Exact, one pass.
2. **hashdiff** — sources in different engines: hierarchical checksum
   bisecting. Segments whose per-segment signatures match are discarded;
   differing segments recurse down to ~8k rows and only those are pulled.
   Cheap when tables are huge and nearly identical.

Output covers four levels (each individually switchable): schema diff, row
counts, cell-level value differences with examples, and per-column statistics
drift. Machine-readable output is versioned JSON.

Verified correctness: every injected deviation of a synthetic ground-truth
generator (13 deviation types) is found exactly; identical inputs always
yield zero differences (property-tested); a Postgres table and its parquet
export diff to zero across engines. Scale: 10M rows x 20 columns in under
60 seconds end-to-end (see BENCHMARKS.md).

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone <this-repo> tabdiff && cd tabdiff
uv sync --dev          # creates .venv, installs everything
uv run tabdiff --version
```

Postgres support needs Docker once to fetch the DuckDB extension
(`INSTALL postgres`, automatic on first use), plus network access to your
database — local formats never touch the network at all (enforced by test).

## Usage

Sources: `file.parquet`, `file.csv`, `duckdb://path/db.duckdb/table`,
`postgres://user:pass@host:port/db/schema/table`.

### 1. Two parquet files (rich output)

```
$ tabdiff diff old/users.parquet new/users.parquet --key id
tabdiff - DIFFERENCES FOUND
strategy=join key=id time=0.06s
...
      Row counts
┌─────────────┬──────┐
│ metric      │ rows │
├─────────────┼──────┤
│ left total  │  300 │
│ right total │  300 │
│ only left   │    0 │
│ only right  │    0 │
│ in both     │  300 │
└─────────────┴──────┘
```

### 2. CI-friendly: versioned JSON + exit codes

```
$ tabdiff diff old.parquet new.parquet --key id --format json | jq .counts
{
  "left_total": 300,
  "right_total": 300,
  "left_only": 0,
  "right_only": 0,
  "both": 300
}
$ echo $?
1        # 0 = identical, 1 = differences, 2 = error
```

### 3. Float tolerance for cross-engine data

```
$ tabdiff diff pg_export.parquet postgres://u:p@host/db/public/events \
    --key event_id --tolerance-rel 1e-9 --assume-tz UTC
```

### 4. Two separate Postgres instances (hashdiff, pushdown)

```
$ tabdiff diff postgres://ro@db-a/prod/sales.orders \
             postgres://ro@db-b/replica/sales.orders \
    --key order_id --strategy hash
tabdiff - IDENTICAL
strategy=hash key=order_id time=12.31s
```

### 5. CSV without reliable typing + text-JSON columns

```
$ tabdiff diff dump_a.csv dump_b.csv --key id \
    --all-varchar --json-columns payload --treat-empty-as-null -f markdown -o report.md
```

More flags: `--tolerance-abs/--tolerance-rel`, `--assume-tz`,
`--ts-precision {coarse,s,ms,us,ns}`, `--examples N`, `--full`,
`--no-schema/--no-counts/--no-values/--no-stats`, `--leaf-rows`,
`--key-less`.

## How this differs from data-diff

| | tabdiff | data-diff (OSS) |
|---|---|---|
| Status 2026 | maintained here | stale; development in commercial cloud |
| Local-first focus | core design (parquet/csv/duckdb first-class) | database-centric |
| Compute model | embedded DuckDB does the heavy lifting | per-database SQL dialects |
| Cross-engine checksums | portable canonical-text SQL, parity property-tested | dialect-specific implementations |
| Output | rich / versioned JSON / markdown | text |
| dbt integration | planned, not built | had partial support |

## Known limits

Read [LIMITS.md](LIMITS.md) before production use. Highlights: float equality
has ~15-significant-digit granularity; JSON key-order handling in cross-engine
hashdiff is unreliable; string comparison is NFC-normalized by design.

## Development

```bash
make check   # ruff format+check, mypy --strict, pytest
make bench   # 10M-row benchmark (writes numbers used in BENCHMARKS.md)
make demo    # small end-to-end demo with generated data
```

See CONTRIBUTING.md. Tests need no services except the marked postgres
integration tests (Docker via testcontainers, or set `TABDIFF_TEST_PG_DSN`).

## License

MIT — see LICENSE.
