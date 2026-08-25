# Contributing to tabdiff

## Setup

```bash
uv sync --dev          # creates .venv with Python 3.12
uv run pytest          # tests
```

Postgres integration tests need Docker (testcontainers) or set
`TABDIFF_TEST_PG_DSN=postgres://user:pass@host:db` to use an existing server.
Tests skip cleanly when neither is available.

## Rules

1. `make check` (or its documented command equivalent) must be green before
   every commit: ruff format, ruff check, `mypy --strict`, pytest.
2. No pandas in the hot path. Heavy lifting happens in DuckDB SQL; Python only
   orchestrates and renders.
3. Behavior changes need a test that fails without the change. Performance
   claims need a number in BENCHMARKS.md.
4. JSON output is versioned (`schema_version`) and additive-only.
5. Document every non-obvious decision in DECISIONS.md; document every cut
   corner in PROGRESS.md ("Not implemented") or LIMITS.md.

## Layout

```
src/tabdiff/       library + CLI
tests/gen.py       synthetic data generator (ground truth for diffs)
tests/             pytest suite (unit, property, integration)
benchmarks/        scale benchmark scripts
```
