"""Command line interface."""

from __future__ import annotations

import traceback
from pathlib import Path

import typer
from rich.console import Console

from tabdiff import __version__
from tabdiff.canon import CompareOptions
from tabdiff.diff import RunOptions, run_diff
from tabdiff.errors import TabDiffError
from tabdiff.report import render_rich, to_json, to_markdown

app = typer.Typer(
    name="tabdiff",
    help="Local-first, cell-level diff for tabular data sources.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

EXIT_IDENTICAL = 0
EXIT_DIFFERENCES = 1
EXIT_ERROR = 2


def _version(version: bool = typer.Option(False, "--version", is_eager=True)) -> None:
    if version:
        print(f"tabdiff {__version__}")
        raise typer.Exit


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", is_eager=True),
) -> None:
    """Local-first diff for tabular data. Nothing leaves your machine."""
    if version:
        print(f"tabdiff {__version__}")
        raise typer.Exit
    if ctx.invoked_subcommand is None:
        raise typer.Exit


@app.command()
def diff(
    left: str = typer.Argument(
        ...,
        help="Left source: file.parquet | file.csv | "
        "duckdb://path/db.duckdb/table | postgres://host/db/schema/table",
    ),
    right: str = typer.Argument(..., help="Right source (same grammar)."),
    key: str | None = typer.Option(None, "--key", help="Comma-separated key columns."),
    key_less: bool = typer.Option(
        False,
        "--key-less",
        help="Compare as multisets of rows (no cell-level attribution possible).",
    ),
    strategy: str = typer.Option("auto", "--strategy", help="join | hash | auto"),
    fmt: str = typer.Option("rich", "--format", "-f", help="rich | json | markdown"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write json/markdown to file."),
    tolerance_abs: float = typer.Option(0.0, "--tolerance-abs"),
    tolerance_rel: float = typer.Option(0.0, "--tolerance-rel"),
    treat_empty_as_null: bool = typer.Option(False, "--treat-empty-as-null"),
    assume_tz: str | None = typer.Option(
        None, "--assume-tz", help="IANA zone to interpret naive timestamps in."
    ),
    ts_precision: str = typer.Option("coarse", "--ts-precision", help="coarse | s | ms | us | ns"),
    json_columns: str | None = typer.Option(
        None, "--json-columns", help="Comma-separated text columns holding JSON."
    ),
    examples: int = typer.Option(20, "--examples", help="Examples per differing column."),
    full: bool = typer.Option(False, "--full", help="Full example output (large!)."),
    all_varchar: bool = typer.Option(False, "--all-varchar", help="Read CSVs without typing."),
    leaf_rows: int = typer.Option(8192, "--leaf-rows", help="hashdiff leaf segment size."),
    no_schema: bool = typer.Option(False, "--no-schema"),
    no_counts: bool = typer.Option(False, "--no-counts"),
    no_values: bool = typer.Option(False, "--no-values"),
    no_stats: bool = typer.Option(False, "--no-stats"),
) -> None:
    """Diff two tabular sources row-by-row and cell-by-cell."""
    compare_opts = CompareOptions(
        tolerance_abs=tolerance_abs,
        tolerance_rel=tolerance_rel,
        treat_empty_as_null=treat_empty_as_null,
        assume_tz=assume_tz,
        ts_precision=ts_precision,
        json_columns=frozenset(s.strip() for s in json_columns.split(","))
        if json_columns
        else frozenset(),
    )
    options = RunOptions(
        key=tuple(k.strip() for k in key.split(",")) if key else (),
        key_less=key_less,
        strategy=strategy,
        opts=compare_opts,
        examples_n=None if full else examples,
        full=full,
        include_schema=not no_schema,
        include_counts=not no_counts,
        include_values=not no_values,
        include_stats=not no_stats,
        all_varchar=all_varchar,
        leaf_rows=leaf_rows,
    )
    try:
        report = run_diff(left, right, options)
    except TabDiffError as exc:
        _stderr().print(f"[red]error[/red] {exc}")
        raise typer.Exit(EXIT_ERROR) from exc
    except Exception as exc:
        _stderr().print(f"[red]unexpected error[/red] {exc!r}")
        traceback.print_exc()
        raise typer.Exit(EXIT_ERROR) from exc

    text_out: str | None = None
    if fmt == "json":
        text_out = to_json(report)
    elif fmt == "markdown":
        text_out = to_markdown(report)
    elif fmt == "rich":
        render_rich(report)
    else:
        _stderr().print(f"[red]error[/red] unknown format {fmt!r}")
        raise typer.Exit(EXIT_ERROR)

    if text_out is not None:
        if output is not None:
            Path(output).write_text(text_out + "\n", encoding="utf-8")
            print(f"report written to {output}")
        else:
            print(text_out)

    raise typer.Exit(EXIT_DIFFERENCES if report.has_differences else EXIT_IDENTICAL)


def _stderr() -> Console:
    return Console(stderr=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
