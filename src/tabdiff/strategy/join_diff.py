"""joindiff: FULL OUTER JOIN on the canonical key, compared entirely in SQL."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tabdiff.canon import (
    CompareOptions,
    canonical_field_sql,
    effective_precision,
    equality_sql,
    escaped_field_sql,
    json_semantic_equal,
)
from tabdiff.keycheck import check_key_usable
from tabdiff.model import (
    CellExample,
    ColumnValueDiff,
    CountsDiff,
    DiffMeta,
    DiffReport,
    ValueDiffResult,
)
from tabdiff.normalize import Canon, PairClass, TypeInfo
from tabdiff.schema_diff import diff_schemas, parse_type

if TYPE_CHECKING:
    from tabdiff.session import Session
    from tabdiff.source.base import BoundSource

# Above this many SQL-level JSON mismatches per column we stop re-checking in
# Python and label the count approximated instead of lying about exactness.
JSON_REFINE_LIMIT = 10_000
# Hard cap for --full example extraction (protects process memory).
FULL_EXAMPLE_CAP = 200_000


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class _Plan:
    sql: str  # SELECT kc, c0..cN FROM source


def _side_sql(
    src: BoundSource,
    key_cols: list[str],
    compare_names: list[str],
    ti_by_name: dict[str, TypeInfo],
    opts: CompareOptions,
) -> _Plan:
    """Inner SELECT producing kc (canonical key text) + raw payload aliases."""
    parts: list[str] = []
    for k in key_cols:
        canon = canonical_field_sql(
            "duckdb",
            _qi(k),
            ti_by_name[k],
            opts,
            target_precision=None,
        )
        parts.append(escaped_field_sql(canon))
    kc = "||".join(parts)
    payloads = ", ".join(f"{_qi(c)} AS {_qi(f'c{i}')}" for i, c in enumerate(compare_names))
    select_list = f"{kc} AS {_qi('kc')}" + (f", {payloads}" if payloads else "")
    return _Plan(sql=f"SELECT {select_list} FROM {src.relation_sql()}")


def run_join_diff(
    session: Session,
    left_src: BoundSource,
    right_src: BoundSource,
    *,
    key_cols: list[str],
    opts: CompareOptions,
    examples_n: int | None = 20,
    full: bool = False,
    include_schema: bool = True,
    include_counts: bool = True,
    include_values: bool = True,
) -> DiffReport:
    started = time.monotonic()
    l_cols = left_src.columns()
    r_cols = right_src.columns()

    schema_diff, pairs = diff_schemas(
        l_cols, r_cols, assume_tz=opts.assume_tz, ts_precision=opts.ts_precision
    )
    assumptions = list(schema_diff.assumptions)
    warnings = list(schema_diff.warnings)

    # ---- keys -----------------------------------------------------------------
    l_ti = {c.name: parse_type(c) for c in l_cols}
    r_ti = {c.name: parse_type(c) for c in r_cols}
    for k in key_cols:
        missing_side = "left" if k not in l_ti else ("right" if k not in r_ti else None)
        if missing_side:
            msg = f"key column {k!r} not present on the {missing_side} side"
            raise KeyError(msg)

    check_key_usable(session, left_src, key_cols, side="left")
    check_key_usable(session, right_src, key_cols, side="right")

    # ---- comparison set ---------------------------------------------------------
    compare_names = [c.name for c in l_cols if c.name in pairs]
    pair_by_name: dict[str, tuple[TypeInfo, TypeInfo, PairClass]] = pairs

    # per-column effective timestamp precision
    precisions = {
        name: effective_precision(lt, rt, opts) for name, (lt, rt, _pc) in pair_by_name.items()
    }

    # key TypeInfos for canonicalization come from each side itself; for
    # temporal keys apply the shared precision so both sides truncate alike.
    l_key_ti = dict(l_ti)
    r_key_ti = dict(r_ti)
    key_prec: int | None = None
    if any(l_key_ti[k].is_temporal or r_key_ti[k].is_temporal for k in key_cols):
        key_prec = min(
            [p for k in key_cols for p in (l_key_ti[k].precision, r_key_ti[k].precision) if p],
            default=None,
        )

    def key_canon(src_ti: dict[str, TypeInfo], k: str) -> str:
        return canonical_field_sql("duckdb", _qi(k), src_ti[k], opts, target_precision=key_prec)

    def build_side(src_ti: dict[str, TypeInfo]) -> str:
        parts = [escaped_field_sql(key_canon(src_ti, k)) for k in key_cols]
        kc = "||".join(parts)
        payloads = ", ".join(f"{_qi(c)} AS {_qi(f'c{i}')}" for i, c in enumerate(compare_names))
        head = f"{kc} AS {_qi('kc')}"
        return f"SELECT {head}{', ' + payloads if payloads else ''} FROM {{rel}}"

    l_inner_tpl = build_side(l_key_ti)
    r_inner_tpl = build_side(r_key_ti)
    l_sql = l_inner_tpl.format(rel=left_src.relation_sql())
    r_sql = r_inner_tpl.format(rel=right_src.relation_sql())

    # ---- equality expressions -----------------------------------------------------
    eq_exprs: list[str] = []
    json_pairs: set[str] = set()
    for i, name in enumerate(compare_names):
        lt, rt, pc = pair_by_name[name]
        interpret = any(t.canon is Canon.TIMESTAMP_NAIVE for t in (lt, rt)) and bool(opts.assume_tz)
        if Canon.JSON in (lt.canon, rt.canon) or name in opts.json_columns:
            # canonicalize both sides through the JSON parser (whitespace-
            # insensitive); key-order handled by the Python refinement pass.
            json_pairs.add(name)
            json_ti = TypeInfo(Canon.JSON, "JSON")
            lc = canonical_field_sql("duckdb", f"L.{_qi(f'c{i}')}", json_ti, opts)
            rc = canonical_field_sql("duckdb", f"R.{_qi(f'c{i}')}", json_ti, opts)
            eq_exprs.append(f"({lc} = {rc})")
            continue
        eq_exprs.append(
            equality_sql(
                "duckdb",
                f"L.{_qi(f'c{i}')}",
                lt,
                f"R.{_qi(f'c{i}')}",
                rt,
                opts,
                pc,
                target_precision=precisions[name],
                interpret_naive_tz=interpret,
            )
        )

    changed_pred = (
        "TRUE"
        if not compare_names
        else "NOT ("
        + " AND ".join(f"COALESCE(e{i}, TRUE)" for i in range(len(compare_names)))
        + ")"
    )
    m_cols = (
        ",\n  ".join(
            f"count(*) FILTER (WHERE inl AND inr AND NOT e{i}) AS m{i}"
            for i in range(len(compare_names))
        )
        or "NULL::BIGINT AS m0"
    )

    agg_sql = f"""
WITH L AS ({l_sql}),
     R AS ({r_sql}),
J AS (
  SELECT
    (L."kc" IS NOT NULL) AS inl,
    (R."kc" IS NOT NULL) AS inr,
    {", ".join(f"{eq} AS e{i}" for i, eq in enumerate(eq_exprs)) if eq_exprs else "TRUE AS e0"},
    L."kc" AS kc
  FROM L FULL OUTER JOIN R ON L."kc" = R."kc"
)
SELECT
  count(*) FILTER (WHERE inl AND NOT inr),
  count(*) FILTER (WHERE inr AND NOT inl),
  count(*) FILTER (WHERE inl AND inr),
  count(*) FILTER (WHERE inl AND inr AND {changed_pred}),
  {m_cols}
FROM J
""".strip()

    row = session.rows(agg_sql)[0]
    counts = CountsDiff(
        left_total=left_src.count(),
        right_total=right_src.count(),
        left_only=int(row[0]),
        right_only=int(row[1]),
        both=int(row[2]),
    )
    changed_rows = int(row[3])
    mismatches = [int(row[4 + i]) for i in range(len(compare_names))]

    value_result = ValueDiffResult(changed_rows=changed_rows)
    limit = None if full else examples_n

    if include_values:
        for i, name in enumerate(compare_names):
            if mismatches[i] == 0:
                continue
            col_diff = ColumnValueDiff(column=name, mismatched_rows=mismatches[i])
            fetch_n = (
                min(mismatches[i], limit)
                if limit is not None
                else min(mismatches[i], FULL_EXAMPLE_CAP)
            )
            base_where = f"NOT {eq_exprs[i]}"
            if fetch_n > 0:
                ex_sql = f"""
SELECT L."kc", CAST(L.{_qi(f"c{i}")} AS VARCHAR), CAST(R.{_qi(f"c{i}")} AS VARCHAR)
FROM ({l_sql}) L JOIN ({r_sql}) R ON L."kc" = R."kc"
WHERE {base_where}
LIMIT {fetch_n}
""".strip()
                for kr, lv, rv in session.rows(ex_sql):
                    col_diff.examples.append(CellExample(str(kr), lv, rv))
            lt, rt, _pc = pair_by_name[name]
            if name in json_pairs:
                col_diff = _refine_json_column(
                    session, l_sql, r_sql, eq_exprs[i], i, col_diff, limit
                )
            value_result.columns.append(col_diff)

    meta = DiffMeta(
        strategy="join",
        key=list(key_cols),
        assumptions=assumptions,
        warnings=warnings,
    )
    report = DiffReport(
        meta=meta,
        schema=schema_diff if include_schema else None,
        counts=counts if include_counts else None,
        values=value_result if include_values else None,
    )
    report.meta.duration_s = time.monotonic() - started
    return report


def _refine_json_column(
    session: Session,
    l_sql: str,
    r_sql: str,
    eq_expr: str,
    i: int,
    col_diff: ColumnValueDiff,
    limit: int | None,
) -> ColumnValueDiff:
    """Re-check SQL-level JSON mismatches semantically in Python.

    The SQL fast path treats reordered JSON keys as different; this pass is
    authoritative. Counts are corrected when the volume is manageable;
    otherwise they are labelled approximated - never silently wrong.
    """
    if col_diff.mismatched_rows > JSON_REFINE_LIMIT:
        col_diff.approximated = True
        return col_diff
    refine_sql = f"""
SELECT CAST(L.{_qi(f"c{i}")} AS VARCHAR), CAST(R.{_qi(f"c{i}")} AS VARCHAR)
FROM ({l_sql}) L JOIN ({r_sql}) R ON L."kc" = R."kc"
WHERE NOT ({eq_expr})
""".strip()
    real = 0
    kept: list[CellExample] = []
    for lv, rv in session.rows(refine_sql):
        if json_semantic_equal(lv or "", rv or ""):
            continue
        real += 1
        room = len(kept) < FULL_EXAMPLE_CAP if limit is None else (limit > 0 and len(kept) < limit)
        if room:
            kept.append(CellExample("", lv, rv))
    col_diff.mismatched_rows = real
    col_diff.examples = kept
    return col_diff


__all__ = ["run_join_diff"]
