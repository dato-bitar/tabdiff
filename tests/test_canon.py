"""Canonical-text parity: DuckDB SQL must match the Python reference.

These tests are the load-bearing wall of cross-engine correctness: whatever
DuckDB produces here is by definition the canonical form, and Postgres is
held to the same standard by the integration tests.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import duckdb as duckdb_mod
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tabdiff.canon import (
    NULL_MARKER,
    CompareOptions,
    canonical_field_sql,
    effective_precision,
    equality_sql,
    py_canonical,
)
from tabdiff.normalize import Verdict, classify, parse_type

OPTS = CompareOptions()


def canon_of(
    value_sql: str,
    type_str: str,
    *,
    engine: Any = "duckdb",
    opts: CompareOptions = OPTS,
    target_precision: int | None = None,
) -> str:
    ti = parse_type(type_str)
    sql = canonical_field_sql(engine, "v", ti, opts, target_precision=target_precision)
    con = duckdb_mod.connect()
    try:
        return str(con.execute(f"SELECT {sql} FROM (SELECT {value_sql} AS v)").fetchone()[0])
    finally:
        con.close()


class TestCanonicalParityWithPythonReference:
    @pytest.mark.parametrize(
        ("value_sql", "type_str", "py_value"),
        [
            ("42::BIGINT", "BIGINT", 42),
            ("-7::BIGINT", "BIGINT", -7),
            ("0::HUGEINT", "HUGEINT", 0),
            ("CAST(1.5 AS DOUBLE)", "DOUBLE", 1.5),
            ("CAST(123456789.5 AS DOUBLE)", "DOUBLE", 123456789.5),
            ("CAST(0.1 AS DOUBLE)", "DOUBLE", 0.1),
            ("CAST(1e20 AS DOUBLE)", "DOUBLE", 1e20),
            ("CAST('inf' AS DOUBLE)", "DOUBLE", float("inf")),
            ("CAST('nan' AS DOUBLE)", "DOUBLE", float("nan")),
            ("DECIMAL(10,2) '12.30'", "DECIMAL(10,2)", Decimal("12.30")),
            ("DECIMAL(10,3) '-0.500'", "DECIMAL(10,3)", Decimal("-0.500")),
            ("DECIMAL(5,0) '120'", "DECIMAL(5,0)", Decimal("120")),
            ("DECIMAL(6,4) '0.0000'", "DECIMAL(6,4)", Decimal("0.0000")),
            ("'héllo wörld'", "VARCHAR", "héllo wörld"),
            ("TRUE", "BOOLEAN", True),
            ("FALSE", "BOOLEAN", False),
            ("DATE '2024-03-01'", "DATE", __import__("datetime").date(2024, 3, 1)),
            (
                "TIMESTAMP '2024-06-01 12:30:45.123456'",
                "TIMESTAMP",
                __import__("datetime").datetime(2024, 6, 1, 12, 30, 45, 123456),
            ),
            (
                "TIMESTAMPTZ '2024-06-01 12:00:00+02'",
                "TIMESTAMP WITH TIME ZONE",
                __import__("datetime").datetime(
                    2024,
                    6,
                    1,
                    10,
                    0,
                    tzinfo=__import__("datetime").timezone.utc,
                ),
            ),
            (
                "UUID '6BA16AAA-1A63-4FB7-93A9-BD4C4C8BCE3F'",
                "UUID",
                __import__("uuid").UUID("6ba16aaa-1a63-4fb7-93a9-bd4c4c8bce3f"),
            ),
            ("'\\xDE\\xAD'::BLOB", "BLOB", b"\xde\xad"),
        ],
    )
    def test_parity(self, value_sql: str, type_str: str, py_value: object) -> None:
        got = canon_of(value_sql, type_str)
        want = py_canonical(py_value, parse_type(type_str), OPTS)
        assert got == want, f"duckdb={got!r} python={want!r}"

    @pytest.mark.parametrize(
        "value_sql",
        [
            "NULL::BIGINT",
            "NULL::VARCHAR",
            "NULL::DOUBLE",
            "NULL::TIMESTAMP",
            "NULL::BOOLEAN",
        ],
    )
    def test_null_marker_everywhere(self, value_sql: str) -> None:
        type_str = value_sql.split("::")[1]
        assert canon_of(value_sql, type_str) == NULL_MARKER


class TestEqualitySemantics:
    def _eq(self, l_sql: str, l_t: str, r_sql: str, r_t: str, **kw: object) -> bool:
        lt, rt = parse_type(l_t), parse_type(r_t)
        pc = classify(lt, rt)
        opts = kw.pop("opts", OPTS)
        con = duckdb_mod.connect()
        try:
            row = con.execute(
                f"SELECT {equality_sql('duckdb', 'l', lt, 'r', rt, opts, pc)} "
                f"FROM (SELECT {l_sql} AS l, {r_sql} AS r)"
            ).fetchone()
            return bool(row[0])
        finally:
            con.close()

    def test_nfc_composition_equal(self) -> None:
        assert self._eq("'café'", "VARCHAR", "'cafe\u0301'", "VARCHAR")

    def test_plain_string_inequality(self) -> None:
        assert not self._eq("'a'", "VARCHAR", "'b'", "VARCHAR")

    def test_empty_vs_null_distinct_by_default(self) -> None:
        assert not self._eq("''", "VARCHAR", "NULL::VARCHAR", "VARCHAR")

    def test_empty_as_null_option(self) -> None:
        opts = CompareOptions(treat_empty_as_null=True)
        assert self._eq("''", "VARCHAR", "NULL::VARCHAR", "VARCHAR", opts=opts)

    def test_bool_aliases_true_forms(self) -> None:
        for lit in ["'yes'", "'t'", "'TRUE'", "'1'", "'Y'"]:
            assert self._eq(lit, "VARCHAR", "TRUE", "BOOLEAN"), lit

    def test_bool_aliases_false_forms(self) -> None:
        for lit in ["'no'", "'F'", "'0'"]:
            assert self._eq(lit, "VARCHAR", "FALSE", "BOOLEAN"), lit

    def test_unknown_bool_alias_never_equal(self) -> None:
        assert not self._eq("'maybe'", "VARCHAR", "TRUE", "BOOLEAN")
        assert not self._eq("'maybe'", "VARCHAR", "NULL::BOOLEAN", "BOOLEAN")

    def test_string_cast_numeric(self) -> None:
        assert self._eq("'123'", "VARCHAR", "123::BIGINT", "BIGINT")
        assert not self._eq("'abc'", "VARCHAR", "123::BIGINT", "BIGINT")

    def test_decimal_exact_equality_despite_scale(self) -> None:
        assert self._eq(
            "DECIMAL(10,2) '0.30'", "DECIMAL(10,2)", "DECIMAL(10,3) '0.300'", "DECIMAL(10,3)"
        )

    def test_float_sum_trap_is_reported_not_hidden(self) -> None:
        # 0.1 + 0.2 != 0.3 in binary floating point; default mode must be exact.
        assert not self._eq("(0.1::DOUBLE + 0.2::DOUBLE)", "DOUBLE", "0.3::DOUBLE", "DOUBLE")

    def test_tolerance_abs(self) -> None:
        opts = CompareOptions(tolerance_abs=0.05)
        assert self._eq("1.0::DOUBLE", "DOUBLE", "1.04::DOUBLE", "DOUBLE", opts=opts)
        assert not self._eq("1.0::DOUBLE", "DOUBLE", "1.2::DOUBLE", "DOUBLE", opts=opts)

    def test_tolerance_rel(self) -> None:
        opts = CompareOptions(tolerance_rel=0.02)
        assert self._eq("100.0::DOUBLE", "DOUBLE", "101.0::DOUBLE", "DOUBLE", opts=opts)
        assert not self._eq("100.0::DOUBLE", "DOUBLE", "103.0::DOUBLE", "DOUBLE", opts=opts)

    def test_tolerance_symmetry_both_directions(self) -> None:
        opts = CompareOptions(tolerance_rel=0.02)
        assert self._eq("101.0::DOUBLE", "DOUBLE", "100.0::DOUBLE", "DOUBLE", opts=opts)

    def test_null_pairs_equal_under_tolerance(self) -> None:
        opts = CompareOptions(tolerance_abs=1.0)
        assert self._eq("NULL::DOUBLE", "DOUBLE", "NULL::DOUBLE", "DOUBLE", opts=opts)
        assert not self._eq("NULL::DOUBLE", "DOUBLE", "1.0::DOUBLE", "DOUBLE", opts=opts)

    def test_timestamp_precision_coarse_truncation(self) -> None:
        # left stores microseconds, right milliseconds; coarse mode truncates both.
        lt, rt = parse_type("TIMESTAMP"), parse_type("TIMESTAMP_MS")
        pc = classify(lt, rt)
        prec = effective_precision(lt, rt, OPTS)
        assert prec == 3
        con = duckdb_mod.connect()
        sql = equality_sql("duckdb", "l", lt, "r", rt, OPTS, pc, target_precision=prec)
        same = con.execute(
            f"SELECT {sql} FROM (SELECT TIMESTAMP '2024-01-01 00:00:00.123456' AS l, "
            "TIMESTAMP_MS '2024-01-01 00:00:00.123' AS r)"
        ).fetchone()[0]
        diff = con.execute(
            f"SELECT {sql} FROM (SELECT TIMESTAMP '2024-01-01 00:00:00.123999' AS l, "
            "TIMESTAMP_MS '2024-01-01 00:00:00.124' AS r)"
        ).fetchone()[0]
        con.close()
        assert same
        assert not diff

    def test_naive_vs_tz_with_assume_tz_berlin(self) -> None:
        opts = CompareOptions(assume_tz="Europe/Berlin")
        # 12:00 naive Berlin summer time == 10:00 UTC (CEST, +02:00 incl. DST)
        assert self._eq(
            "TIMESTAMP '2024-06-01 12:00:00'",
            "TIMESTAMP",
            "TIMESTAMPTZ '2024-06-01 10:00:00Z'",
            "TIMESTAMP WITH TIME ZONE",
            opts=opts,
        )
        assert not self._eq(
            "TIMESTAMP '2024-06-01 12:00:00'",
            "TIMESTAMP",
            "TIMESTAMPTZ '2024-06-01 11:00:00Z'",
            "TIMESTAMP WITH TIME ZONE",
            opts=opts,
        )

    def test_dst_boundary_respected_by_assume_tz(self) -> None:
        opts = CompareOptions(assume_tz="Europe/Berlin")
        # winter: CET = +1, so 12:00 Berlin == 11:00 UTC in January
        assert self._eq(
            "TIMESTAMP '2024-01-15 12:00:00'",
            "TIMESTAMP",
            "TIMESTAMPTZ '2024-01-15 11:00:00Z'",
            "TIMESTAMP WITH TIME ZONE",
            opts=opts,
        )

    def test_json_whitespace_insensitive_fast_path(self) -> None:
        assert self._eq("""'{"a": 1, "b": [1, 2]}'""", "JSON", """'{"a":1,"b":[1,2]}'""", "JSON")


# ---------------------------------------------------------------------------
# property tests: pin the SQL canonicalizer to the Python reference
# ---------------------------------------------------------------------------

safe_text = st.text(min_size=0, max_size=60).filter(lambda s: "\x00" not in s and s != "")


def sql_str_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


@settings(max_examples=100, deadline=None)
@given(safe_text)
def test_property_nfc_matches_python(s: str) -> None:
    got = canon_of(sql_str_literal(s), "VARCHAR")
    assert got == py_canonical(s, parse_type("VARCHAR"), OPTS)


finite_floats = st.floats(allow_nan=False, allow_infinity=False, width=64)


@settings(max_examples=100, deadline=None)
@given(finite_floats)
def test_property_double_canonical_is_idempotent_under_reparse(f: float) -> None:
    """DuckDB's float text allows ~1 ULP slop, so we pin *stability* instead
    of byte-parity with Python: whatever DuckDB prints must re-parse to a
    value whose canonical form is identical - otherwise equality would not be
    transitive."""
    c1 = canon_of(f"({f!r})::DOUBLE", "DOUBLE")
    c2 = canon_of(f"({c1})::DOUBLE", "DOUBLE")
    assert c1 == c2


@settings(max_examples=50, deadline=None)
@given(st.integers(min_value=-(2**62), max_value=2**62))
def test_property_bigint_canonical_matches_python(i: int) -> None:
    got = canon_of(f"({i})::BIGINT", "BIGINT")
    assert got == py_canonical(i, parse_type("BIGINT"), OPTS)


naive_datetimes = st.datetimes(
    min_value=__import__("datetime").datetime(1900, 1, 1),
    max_value=__import__("datetime").datetime(2999, 12, 31, 23, 59, 59, 999999),
)


@settings(max_examples=75, deadline=None)
@given(naive_datetimes)
def test_property_timestamp_canonical_matches_python(dt: Any) -> None:
    micro = dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    got = canon_of(f"TIMESTAMP '{micro}'", "TIMESTAMP")
    assert got == py_canonical(dt, parse_type("TIMESTAMP"), OPTS)


def test_verdict_enum_stable() -> None:
    assert {v.value for v in Verdict} == {
        "same",
        "benign",
        "widening",
        "lossy",
        "needs_tz",
        "string_cast",
        "bool_alias",
        "incompatible",
    }
