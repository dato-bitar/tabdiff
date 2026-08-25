"""Column-statistics drift: min/max/avg/nulls/distinct, left vs right.

Computed in ONE aggregate query per side so it costs a single scan even on
large tables. Informational by design: distribution drift is reported even
when keys and values match exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tabdiff.model import ColumnStats, StatsDrift
from tabdiff.normalize import Canon

if TYPE_CHECKING:
    from tabdiff.session import Session
    from tabdiff.source.base import BoundSource


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _agg_expr(name: str, canon: Canon | None) -> list[str]:
    q = _qi(name)
    parts = [
        f"count({q})",
        f"CASE WHEN count({q}) = 0 THEN NULL ELSE CAST(min({q}) AS VARCHAR) END",
        f"CASE WHEN count({q}) = 0 THEN NULL ELSE CAST(max({q}) AS VARCHAR) END",
        f"count(DISTINCT {q})",
    ]
    if canon in {Canon.INTEGER, Canon.DECIMAL, Canon.FLOAT}:
        parts.append(
            f"CASE WHEN count({q}) = 0 THEN NULL ELSE CAST(avg(CAST({q} AS DOUBLE)) AS VARCHAR) END"
        )
    else:
        parts.append("NULL")
    return parts


def compute_side_stats(
    session: Session,
    src: BoundSource,
    columns: list[tuple[str, Canon | None]],
) -> dict[str, ColumnStats]:
    """One-scan aggregate of per-column statistics."""
    exprs: list[str] = ["count(*)"]
    layout: list[tuple[str, int]] = []  # (name, offset into result row)
    base = 1
    for name, canon in columns:
        aggs = _agg_expr(name, canon)
        layout.append((name, base))
        exprs.extend(aggs)
        base += len(aggs)
    sql = f"SELECT {', '.join(exprs)} FROM {src.relation_sql()}"
    row = session.rows(sql)[0]
    out: dict[str, ColumnStats] = {}
    total = int(row[0])
    for name, off in layout:
        null_count = total - int(row[off])
        stats = ColumnStats(
            min_=None if row[off + 1] is None else str(row[off + 1]),
            max_=None if row[off + 2] is None else str(row[off + 2]),
            avg=None if row[off + 4] is None else str(row[off + 4]),
            null_count=null_count,
            distinct_count=int(row[off + 3]),
        )
        out[name] = stats
    return out


def compute_stats_drift(
    session: Session,
    left_src: BoundSource,
    right_src: BoundSource,
    column_types: dict[str, Canon | None],
) -> StatsDrift:
    """Statistics for shared columns on both sides, side by side."""
    names = sorted(column_types)
    l_stats = compute_side_stats(session, left_src, [(n, column_types[n]) for n in names])
    r_stats = compute_side_stats(session, right_src, [(n, column_types[n]) for n in names])
    return StatsDrift(columns={n: (l_stats[n], r_stats[n]) for n in names})


__all__ = ["compute_side_stats", "compute_stats_drift"]
