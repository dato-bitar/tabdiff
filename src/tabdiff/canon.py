"""Canonical value representation - the single source of truth for equality.

Two values are equal iff their canonical TEXT forms are equal, except for
tolerance-compared numerics which get dedicated SQL. The same canonical text
feeds hashdiff row hashes, so join- and hash-strategies cannot disagree.

Every rule here exists twice (DuckDB + Postgres SQL) and once more as a
Python reference used by property tests to pin the DuckDB behaviour.
"""

from __future__ import annotations

import json
import unicodedata
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Literal, cast

from tabdiff.normalize import Canon, PairClass, TypeInfo, Verdict

Engine = Literal["duckdb", "postgres"]

NULL_MARKER = "\x01TABDIFF_NULL\x01"
NULL_MARKER_SQL = "(chr(1)||'TABDIFF_NULL'||chr(1))"
BAD_BOOL_MARKER = "\x02TABDIFF_BADBOOL\x02"

_TS_FMT_DUCKDB = "'%Y-%m-%d %H:%M:%S.%f'"
_TS_FMT_PG = "'YYYY-MM-DD HH24:MI:SS.US'"
_TRUNC_UNIT = {0: "second", 3: "millisecond", 6: "microsecond"}


@dataclass(frozen=True)
class CompareOptions:
    tolerance_abs: float = 0.0
    tolerance_rel: float = 0.0
    treat_empty_as_null: bool = False
    assume_tz: str | None = None  # IANA name, e.g. Europe/Berlin
    ts_precision: str = "coarse"  # coarse | s | ms | us | ns
    # Columns whose *text* content is JSON and must be compared semantically
    # (--json-columns). Parquet/CSV have no reliable JSON logical type.
    json_columns: frozenset[str] = frozenset()


# --------------------------------------------------------------------------
# small portable building blocks
# --------------------------------------------------------------------------


def nfc_wrap(engine: Engine, expr: str) -> str:
    return f"nfc_normalize({expr})" if engine == "duckdb" else f"NORMALIZE({expr})"


def empty_as_null_wrap(expr: str) -> str:
    return f"(CASE WHEN {expr} = '' THEN NULL ELSE {expr} END)"


def canonical_bool_alias_text(expr: str) -> str:
    """Full canonical TEXT for a varchar column holding boolean aliases."""
    low = f"lower(trim({expr}))"
    return (
        f"(CASE WHEN {expr} IS NULL THEN {NULL_MARKER_SQL} "
        f"WHEN {low} IN ('true','t','yes','y','1') THEN 'true' "
        f"WHEN {low} IN ('false','f','no','n','0') THEN 'false' "
        f"ELSE (chr(2)||'TABDIFF_BADBOOL'||chr(2)) END)"
    )


def try_cast_wrap(expr: str, ti: TypeInfo) -> str:
    return f"TRY_CAST({expr} AS {ti.duckdb_type})"


def _float_text_wrap(engine: Engine, expr: str) -> str:
    """Shortest-repr text with cross-engine smoothing of trivial differences."""
    s = f"lower(CAST({expr} AS VARCHAR))"
    s = f"replace({s}, 'infinity', 'inf')"
    s = f"replace({s}, 'e+', 'e')"
    # strip trailing '.0' (pg prints '1', duckdb '1.0') incl. before exponents;
    # LIKE instead of ends_with because postgres has no ends_with()
    s = (
        f"(CASE WHEN {s} LIKE '%.0' THEN left({s}, length({s}) - 2) "
        f"ELSE replace({s}, '.0e', 'e') END)"
    )
    return s


def _decimal_trim_wrap(expr: str) -> str:
    s = f"CAST({expr} AS VARCHAR)"
    inner = f"(CASE WHEN strpos({s}, '.') > 0 THEN rtrim(rtrim({s}, '0'), '.') ELSE {s} END)"
    return f"(CASE WHEN {inner} = '-0' THEN '0' ELSE {inner} END)"


def _time_pad_wrap(expr: str) -> str:
    """Normalize TIME text to fixed microsecond fraction."""
    s = f"CAST({expr} AS VARCHAR)"
    return f"(CASE WHEN strpos({s}, '.') > 0 THEN rpad({s}, 15, '0') ELSE {s} || '.000000' END)"


def _ts_truncate(engine: Engine, expr: str, target: int) -> str:
    unit = _TRUNC_UNIT.get(target)
    if unit is None:
        return expr
    return f"date_trunc('{unit}', {expr})"


def _ts_format(engine: Engine, expr: str) -> str:
    fmt = _TS_FMT_DUCKDB if engine == "duckdb" else _TS_FMT_PG
    return f"strftime({expr}, {fmt})" if engine == "duckdb" else f"to_char({expr}, {fmt})"


def effective_precision(l_ti: TypeInfo, r_ti: TypeInfo, opts: CompareOptions) -> int | None:
    """Sub-second digits both sides are truncated to before formatting."""
    if opts.ts_precision != "coarse":
        return {"s": 0, "ms": 3, "us": 6, "ns": 9}[opts.ts_precision]
    precs = [
        ti.precision
        for ti in (l_ti, r_ti)
        if ti.canon in {Canon.TIMESTAMP_NAIVE, Canon.TIMESTAMP_TZ}
    ]
    return min(precs) if precs else None


# --------------------------------------------------------------------------
# canonical field text (never NULL - NULL becomes the marker)
# --------------------------------------------------------------------------


def canonical_field_sql(
    engine: Engine,
    expr: str,
    ti: TypeInfo,
    opts: CompareOptions,
    *,
    target_precision: int | None = None,
    interpret_naive_tz: bool = False,
) -> str:
    body = expr
    if opts.treat_empty_as_null and ti.canon is Canon.STRING:
        body = empty_as_null_wrap(body)

    c = ti.canon
    if c is Canon.BOOLEAN:
        text = f"(CASE WHEN {body} THEN 'true' ELSE 'false' END)"
    elif c is Canon.INTEGER:
        text = f"CAST({body} AS VARCHAR)"
    elif c is Canon.DECIMAL:
        text = _decimal_trim_wrap(body)
    elif c is Canon.FLOAT:
        text = _float_text_wrap(engine, body)
    elif c is Canon.STRING:
        text = nfc_wrap(engine, body)
    elif c is Canon.BINARY:
        text = f"lower(hex({body}))" if engine == "duckdb" else f"lower(encode({body}, 'hex'))"
    elif c is Canon.DATE:
        text = f"CAST({body} AS VARCHAR)"
    elif c is Canon.TIME:
        text = _time_pad_wrap(body)
    elif c is Canon.TIMESTAMP_TZ:
        x = f"({body}) AT TIME ZONE 'UTC'"
        if target_precision is not None:
            x = _ts_truncate(engine, x, target_precision)
        text = _ts_format(engine, x)
    elif c is Canon.TIMESTAMP_NAIVE:
        if interpret_naive_tz and opts.assume_tz:
            x = f"(({body}) AT TIME ZONE '{opts.assume_tz}') AT TIME ZONE 'UTC'"
        else:
            x = f"({body})"
        if target_precision is not None:
            x = _ts_truncate(engine, x, target_precision)
        text = _ts_format(engine, x)
    elif c is Canon.UUID:
        text = f"lower(CAST({body} AS VARCHAR))"
    elif c is Canon.JSON:
        # Fast path: whitespace-insensitive, NOT key-order-insensitive in
        # DuckDB (PG jsonb sorts). Final verdicts refined in Python; see
        # LIMITS.md.
        text = (
            f"CAST(to_json(CAST({body} AS JSON)) AS VARCHAR)"
            if engine == "duckdb"
            else f"CAST(CAST({body} AS JSONB) AS TEXT)"
        )
    else:
        text = f"COALESCE(CAST({body} AS VARCHAR), '')"

    return f"(CASE WHEN {body} IS NULL THEN {NULL_MARKER_SQL} ELSE {text} END)"


def escaped_field_sql(field_canonical_sql: str) -> str:
    """Length-prefix so concatenations cannot collide across rows."""
    f = field_canonical_sql
    return f"(CAST(length({f}) AS VARCHAR)||':'||{f})"


# --------------------------------------------------------------------------
# equality
# --------------------------------------------------------------------------


def tolerant_numeric_eq(l_expr: str, r_expr: str, opts: CompareOptions) -> str:
    lv = f"CAST({l_expr} AS DOUBLE)"
    rv = f"CAST({r_expr} AS DOUBLE)"
    abs_part = f"abs({lv} - {rv}) <= {opts.tolerance_abs!r}"
    rel_l = f"abs({lv} - {rv}) / abs({rv})"
    rel_r = f"abs({lv} - {rv}) / abs({lv})"
    rel_part = (
        f"((abs({rv}) > 0 AND {rel_l} <= {opts.tolerance_rel!r}) "
        f"OR (abs({lv}) > 0 AND {rel_r} <= {opts.tolerance_rel!r}))"
    )
    both_null = f"({l_expr} IS NULL AND {r_expr} IS NULL)"
    both_present = f"({l_expr} IS NOT NULL AND {r_expr} IS NOT NULL)"
    return f"({both_null} OR ({both_present} AND ({abs_part} OR {rel_part})))"


def equality_sql(
    engine: Engine,
    l_expr: str,
    l_ti: TypeInfo,
    r_expr: str,
    r_ti: TypeInfo,
    opts: CompareOptions,
    verdict_class: PairClass,
    *,
    target_precision: int | None = None,
    interpret_naive_tz: bool = False,
) -> str:
    vc: Verdict = verdict_class.verdict

    if l_ti.is_numeric and r_ti.is_numeric and (opts.tolerance_abs > 0 or opts.tolerance_rel > 0):
        return tolerant_numeric_eq(l_expr, r_expr, opts)

    interpret = interpret_naive_tz or vc is Verdict.NEEDS_TZ

    if vc is Verdict.BOOL_ALIAS:
        # The alias mapping produces canonical text directly (a typed CASE
        # mixing VARCHAR and BOOLEAN branches is not portable).
        if l_ti.canon is Canon.STRING:
            lc = canonical_bool_alias_text(l_expr)
            rc = canonical_field_sql(engine, r_expr, r_ti, opts, target_precision=target_precision)
        else:
            rc = canonical_bool_alias_text(r_expr)
            lc = canonical_field_sql(engine, l_expr, l_ti, opts, target_precision=target_precision)
        return f"({lc} = {rc})"

    le, re_ = l_expr, r_expr
    le_ti, re_ti = l_ti, r_ti
    if vc is Verdict.STRING_CAST:
        # the string side is cast to the other side's type before canonicalizing
        if l_ti.canon is Canon.STRING:
            le = try_cast_wrap(le, r_ti)
            le_ti = r_ti
        else:
            re_ = try_cast_wrap(re_, l_ti)
            re_ti = l_ti

    lc = canonical_field_sql(
        engine,
        le,
        le_ti,
        opts,
        target_precision=target_precision,
        interpret_naive_tz=interpret,
    )
    rc = canonical_field_sql(
        engine,
        re_,
        re_ti,
        opts,
        target_precision=target_precision,
        interpret_naive_tz=interpret,
    )
    return f"({lc} = {rc})"


# --------------------------------------------------------------------------
# Python reference (mirrors the DuckDB SQL above; property-tested against it)
# --------------------------------------------------------------------------


def _py_float_text(v: float) -> str:
    s = repr(float(v)).lower()
    s = s.replace("infinity", "inf")
    s = s.replace("e+", "e")
    if s.endswith(".0"):
        s = s[:-2]
    return s.replace(".0e", "e")


def _py_decimal_trim(v: Decimal) -> str:
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def py_canonical(
    value: object,
    ti: TypeInfo,
    opts: CompareOptions,
    *,
    target_precision: int | None = None,
    interpret_naive_tz: bool = False,
) -> str:
    if value is None:
        return NULL_MARKER
    c = ti.canon
    if c is Canon.BOOLEAN:
        return "true" if bool(value) else "false"
    if c is Canon.INTEGER:
        return str(value)  # arrow delivers Python ints
    if c is Canon.DECIMAL:
        return _py_decimal_trim(value if isinstance(value, Decimal) else Decimal(str(value)))
    if c is Canon.FLOAT:
        return _py_float_text(float(str(value)))
    if c is Canon.STRING:
        s = str(value)
        if opts.treat_empty_as_null and s == "":
            return NULL_MARKER
        return unicodedata.normalize("NFC", s)
    if c is Canon.BINARY:
        return cast("bytes", value).hex()
    if c is Canon.DATE:
        return cast("date", value).isoformat()
    if c is Canon.TIME:
        t: time = value  # type: ignore[assignment]
        return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}.{t.microsecond:06d}"
    if c is Canon.TIMESTAMP_NAIVE or c is Canon.TIMESTAMP_TZ:
        dt: datetime = value  # type: ignore[assignment]
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        elif interpret_naive_tz and opts.assume_tz:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            dt = dt.replace(tzinfo=ZoneInfo(opts.assume_tz)).astimezone(UTC).replace(tzinfo=None)
        if target_precision is not None and target_precision < 6:
            drop = 6 - target_precision
            dt = dt.replace(microsecond=(dt.microsecond // 10**drop) * 10**drop)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    if c is Canon.UUID:
        return str(uuid_mod.UUID(str(value))).lower()
    if c is Canon.JSON:
        return (
            json.dumps(_sorted_json(value), separators=(",", ":"))
            if not isinstance(value, str)
            else json.dumps(_sorted_json(json.loads(str(value))), separators=(",", ":"))
        )
    return str(value)


def _sorted_json(v: object) -> object:
    if isinstance(v, dict):
        return {k: _sorted_json(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        return [_sorted_json(x) for x in v]
    return v


def json_semantic_equal(a: str, b: str) -> bool:
    """Authoritative Python-side JSON equality (key order/whitespace free)."""
    try:
        return _sorted_json(json.loads(a)) == _sorted_json(json.loads(b))
    except (ValueError, TypeError):
        return a == b
