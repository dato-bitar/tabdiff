"""Report rendering: versioned JSON, markdown, and rich terminal output."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from rich.console import Console
from rich.table import Table as RichTable

from tabdiff.model import SCHEMA_VERSION, DiffReport


def _dataclass_to_dict(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in asdict(cast(Any, obj)).items()}
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dataclass_to_dict(x) for x in obj]
    return obj


def to_json(report: DiffReport, *, indent: int | None = 2) -> str:
    """Stable, versioned JSON output. Existing fields never change shape."""
    doc = {
        "schema_version": SCHEMA_VERSION,
        "identical": report.identical,
        "meta": {
            **_dataclass_to_dict(report.meta),
        },
    }
    if report.schema is not None:
        doc["schema"] = _dataclass_to_dict(report.schema)
    if report.counts is not None:
        doc["counts"] = _dataclass_to_dict(report.counts)
    if report.values is not None:
        doc["values"] = _dataclass_to_dict(report.values)
    if report.stats is not None:
        doc["stats"] = _dataclass_to_dict(report.stats)
    return json.dumps(doc, indent=indent, ensure_ascii=False, sort_keys=False)


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def to_markdown(report: DiffReport) -> str:
    lines: list[str] = []
    verdict = "**IDENTICAL**" if report.identical else "**DIFFERENCES FOUND**"
    lines.append(f"# tabdiff result - {verdict}")
    lines.append("")
    key_disp = ", ".join(report.meta.key) if report.meta.key else "(none)"
    lines.append(f"- Strategy: `{report.meta.strategy}`")
    lines.append(f"- Key: `{key_disp}`")
    lines.append(f"- Duration: {report.meta.duration_s:.2f}s")
    if report.meta.execution_path:
        ep = ", ".join(
            f"{side}={path}" for side, path in sorted(report.meta.execution_path.items())
        )
        lines.append(f"- Execution: {ep}")
    for a in report.meta.assumptions:
        lines.append(f"- Assumption: {a}")
    for w in report.meta.warnings:
        lines.append(f"- Warning: {w}")

    if report.schema is not None:
        lines += ["", "## Schema", ""]
        rows: list[tuple[str, str, str, str, str]] = [
            (
                c.name,
                c.status,
                c.left_type or "-",
                c.right_type or "-",
                c.note or "",
            )
            for c in report.schema.columns
        ]
        if report.schema.order_changed:
            lines.append("_Note: column order differs between sides._")
        lines.append(_md_table(("Column", "Status", "Left", "Right", "Note"), rows))

    counts = report.counts
    if counts is not None:
        lines += ["", "## Row counts", ""]
        lines.append(
            _md_table(
                ("Metric", "Rows"),
                [
                    ("left total", counts.left_total),
                    ("right total", counts.right_total),
                    ("only left", counts.left_only),
                    ("only right", counts.right_only),
                    ("in both", counts.both),
                ],
            )
        )

    values = report.values
    if values is not None:
        lines += ["", "## Value differences", ""]
        lines.append(f"Changed rows: **{values.changed_rows}**")
        if values.columns:
            lines.append("")
            vc_rows: list[tuple[str, str, str]] = [
                (c.column, str(c.mismatched_rows), str(len(c.examples))) for c in values.columns
            ]
            lines.append(_md_table(("Column", "Mismatching rows", "Examples shown"), vc_rows))
        for c in values.columns:
            if c.examples:
                lines += ["", f"### Examples - {c.column}", ""]
                ex_rows = [
                    (
                        e.key,
                        e.left if e.left is not None else "NULL",
                        e.right if e.right is not None else "NULL",
                    )
                    for e in c.examples[:10]
                ]
                lines.append(_md_table(("Key", "Left", "Right"), ex_rows))

    stats = report.stats
    if stats is not None and stats.columns:
        lines += ["", "## Column statistics (left vs right)", ""]
        srows = []
        for name, (lstat, rstat) in sorted(stats.columns.items()):
            srows.append(
                (
                    name,
                    f"{_fmt(lstat.min_)}/{_fmt(rstat.min_)}",
                    f"{_fmt(lstat.max_)}/{_fmt(rstat.max_)}",
                    f"{_fmt(lstat.avg)}/{_fmt(rstat.avg)}",
                    f"{_fmt(lstat.null_count)}/{_fmt(rstat.null_count)}",
                    f"{_fmt(lstat.distinct_count)}/{_fmt(rstat.distinct_count)}",
                )
            )
        lines.append(
            _md_table(
                ("Column", "min L/R", "max L/R", "avg L/R", "nulls L/R", "distinct L/R"),
                srows,
            )
        )

    lines.append("")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def _md_table(header: tuple[Any, ...], rows: list[tuple[Any, ...]]) -> str:
    head = "| " + " | ".join(str(h) for h in header) + " |"
    sep = "|" + "|".join("---" for _ in header) + "|"
    body = "\n".join("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return "\n".join([head, sep, body])


def _cell(value: Any) -> str:
    s = str(value).replace("|", "\\|").replace("\n", " ")
    return s if s else " "


# ---------------------------------------------------------------------------
# rich terminal rendering
# ---------------------------------------------------------------------------


def render_rich(report: DiffReport) -> None:
    console = Console()
    verdict = "[green]IDENTICAL[/green]" if report.identical else "[red]DIFFERENCES FOUND[/red]"
    key_disp = ", ".join(report.meta.key) if report.meta.key else "(none)"
    console.print(f"[bold]tabdiff[/bold] - {verdict}")
    console.print(
        f"strategy=[cyan]{report.meta.strategy}[/cyan] key={key_disp} "
        f"time={report.meta.duration_s:.2f}s"
    )
    if report.meta.execution_path:
        ep = ", ".join(
            f"{side}={path}" for side, path in sorted(report.meta.execution_path.items())
        )
        console.print(f"[dim]execution: {ep}[/dim]")

    for a in report.meta.assumptions:
        console.print(f"[yellow]assumption[/yellow] {a}")
    for w in report.meta.warnings:
        console.print(f"[red]warning[/red] {w}")

    if report.schema is not None:
        t = RichTable(title="Schema")
        for col in ("column", "status", "left type", "right type", "note"):
            t.add_column(col)
        for c in report.schema.columns:
            color = {
                "same": "",
                "benign": "dim",
                "only_left": "red",
                "only_right": "red",
                "incompatible": "red",
            }.get(c.status, "yellow")
            text = f"[{color}]{c.name} [/{color}]" if color else c.name
            t.add_row(text, c.status, c.left_type or "-", c.right_type or "-", c.note)
        console.print(t)

    counts = report.counts
    if counts is not None:
        t = RichTable(title="Row counts")
        t.add_column("metric")
        t.add_column("rows", justify="right")
        t.add_row("left total", str(counts.left_total))
        t.add_row("right total", str(counts.right_total))
        t.add_row("only left", str(counts.left_only), style="red" if counts.left_only else "")
        t.add_row("only right", str(counts.right_only), style="red" if counts.right_only else "")
        t.add_row("in both", str(counts.both))
        console.print(t)

    values = report.values
    if values is not None and values.columns:
        t = RichTable(title=f"Value differences ({values.changed_rows} changed rows)")
        t.add_column("column")
        t.add_column("mismatching rows", justify="right")
        t.add_column("examples shown", justify="right")
        for vc in values.columns:
            suffix = " (~)" if vc.approximated else ""
            t.add_row(vc.column, f"{vc.mismatched_rows}{suffix}", str(len(vc.examples)))
        console.print(t)
        shown = 0
        for vc in values.columns:
            for e in vc.examples:
                if shown >= 20:
                    break
                lv = "NULL" if e.left is None else e.left
                rv = "NULL" if e.right is None else e.right
                console.print(f"  {vc.column}[{e.key}] : {lv!r} -> {rv!r}")
                shown += 1

    stats = report.stats
    if stats is not None and stats.columns:
        t = RichTable(title="Column statistics (left / right)")
        t.add_column("column")
        for col in ("min", "max", "avg", "nulls", "distinct"):
            t.add_column(col)
        for name, (ls, rs) in sorted(stats.columns.items()):
            t.add_row(
                name,
                f"{_fmt(ls.min_)} / {_fmt(rs.min_)}",
                f"{_fmt(ls.max_)} / {_fmt(rs.max_)}",
                f"{_fmt(ls.avg)} / {_fmt(rs.avg)}",
                f"{_fmt(ls.null_count)} / {_fmt(rs.null_count)}",
                f"{_fmt(ls.distinct_count)} / {_fmt(rs.distinct_count)}",
            )
        console.print(t)


__all__ = ["render_rich", "to_json", "to_markdown"]
