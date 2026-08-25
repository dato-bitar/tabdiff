"""key-less mode: compare rows as multisets of whole-row hashes.

Without a key we cannot say *which cell* changed - only that row X exists on
one side but not the other. The report says so explicitly.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from tabdiff.canon import canonical_field_sql, escaped_field_sql
from tabdiff.model import CountsDiff, DiffMeta, DiffReport
from tabdiff.normalize import parse_type
from tabdiff.schema_diff import diff_schemas

if TYPE_CHECKING:
    from tabdiff.canon import CompareOptions
    from tabdiff.session import Session
    from tabdiff.source.base import BoundSource


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def row_multiset_sql(
    session: Session,
    src: BoundSource,
    opts: CompareOptions,
) -> tuple[dict[str, int], list[Any]]:
    """Return {row_hash: count} computed inside the engine."""
    cols = src.columns()
    parts = []
    for c in cols:
        canon = canonical_field_sql("duckdb", _qi(c.name), _ti(c.type), opts)
        parts.append(escaped_field_sql(canon))
    sql = f"SELECT md5({'||'.join(parts)}) AS h, count(*) AS n FROM {src.relation_sql()} GROUP BY 1"
    rows = session.rows(sql)
    return {str(r[0]): int(r[1]) for r in rows}, cols


def _ti(type_str: str) -> Any:
    return parse_type(type_str)


def run_keyless_diff(
    session: Session,
    left_src: BoundSource,
    right_src: BoundSource,
    *,
    opts: CompareOptions,
    include_schema: bool = True,
    include_counts: bool = True,
) -> DiffReport:
    started = time.monotonic()
    l_cols = left_src.columns()
    r_cols = right_src.columns()

    schema_diff, _pairs = diff_schemas(
        l_cols, r_cols, assume_tz=opts.assume_tz, ts_precision=opts.ts_precision
    )
    # The row hash covers ALL columns of each side; schema differences are
    # reported loudly above. Rows are hashed per side independently.
    l_map, _ = row_multiset_sql(session, left_src, opts)
    r_map, _ = row_multiset_sql(session, right_src, opts)

    only_left = sum(n for h, n in l_map.items() if h not in r_map)
    only_right = sum(n for h, n in r_map.items() if h not in l_map)
    matched = sum(min(n, r_map[h]) for h, n in l_map.items() if h in r_map)

    counts = CountsDiff(
        left_total=sum(l_map.values()),
        right_total=sum(r_map.values()),
        left_only=only_left,
        right_only=only_right,
        both=matched,
    )

    meta = DiffMeta(
        strategy="keyless",
        key=[],
        warnings=[
            *schema_diff.warnings,
            "key-less mode: comparing rows as multisets - can only say which "
            "rows are missing/new, never WHICH CELL changed",
        ],
        assumptions=list(schema_diff.assumptions),
    )
    report = DiffReport(
        meta=meta,
        schema=schema_diff if include_schema else None,
        counts=counts if include_counts else None,
        values=None,
    )
    report.meta.duration_s = time.monotonic() - started
    return report


__all__ = ["run_keyless_diff"]
