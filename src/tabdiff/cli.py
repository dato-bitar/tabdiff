"""Command line interface (grows over the milestones)."""

from __future__ import annotations

import typer

from tabdiff import __version__

app = typer.Typer(
    name="tabdiff",
    help="Local-first, cell-level diff for tabular data sources.",
    no_args_is_help=True,
    add_completion=False,
)


def version_callback() -> None:
    print(f"tabdiff {__version__}")
    raise typer.Exit


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """Local-first diff for tabular data. Nothing leaves your machine."""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
