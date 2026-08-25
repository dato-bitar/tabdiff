.PHONY: check fmt test bench demo clean

check:            ## ruff format-check + ruff lint + mypy --strict + pytest
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run pytest

fmt:              ## auto-format and autofix lint
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest

bench:            ## 10M-row scale benchmark (writes BENCHMARKS.md numbers)
	uv run python benchmarks/run_benchmark.py

demo:             ## generate two small parquet files and diff them
	uv run python -m tests.gen demo

clean:
	uv run python -c "import pathlib,shutil; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
