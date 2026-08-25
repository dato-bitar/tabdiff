"""Schema diff behaviour tests."""

from __future__ import annotations

from tabdiff.schema_diff import diff_schemas
from tabdiff.source.base import ColumnInfo


def cols(*specs: tuple[str, str], nullable: bool = True) -> list[ColumnInfo]:
    return [ColumnInfo(n, t, nullable=nullable) for n, t in specs]


def test_identical_schemas() -> None:
    left = cols(("id", "BIGINT"), ("name", "VARCHAR"))
    d, pairs = diff_schemas(left, cols(("id", "BIGINT"), ("name", "VARCHAR")))
    assert d.identical
    assert set(pairs) == {"id", "name"}
    assert not d.warnings


def test_only_left_and_right_blocking() -> None:
    d, pairs = diff_schemas(
        cols(("a", "BIGINT"), ("b", "VARCHAR")),
        cols(("a", "BIGINT"), ("c", "VARCHAR")),
    )
    assert d.has_blocking
    assert {c.name for c in d.by_status("only_left")} == {"b"}
    assert {c.name for c in d.by_status("only_right")} == {"c"}
    assert set(pairs) == {"a"}


def test_incompatible_types_warn_and_skip() -> None:
    d, pairs = diff_schemas(cols(("x", "BLOB")), cols(("x", "BIGINT")))
    assert not d.has_blocking  # blob vs int is per-column incompatible...
    x = next(c for c in d.columns if c.name == "x")
    assert x.status == "incompatible"
    assert "x" not in pairs
    assert any("NOT compared" in w for w in d.warnings)


def test_decimal_vs_float_lossy_but_compared() -> None:
    d, pairs = diff_schemas(cols(("amount", "DECIMAL(18,2)")), cols(("amount", "DOUBLE")))
    col = d.columns[0]
    assert col.status == "lossy"
    assert "amount" in pairs
    assert any("float" in w.lower() for w in d.warnings)


def test_naive_vs_tz_without_assumption() -> None:
    d, pairs = diff_schemas(
        cols(("ts", "TIMESTAMP")),
        cols(("ts", "TIMESTAMP WITH TIME ZONE")),
    )
    assert d.columns[0].status == "needs_tz"
    assert "ts" not in pairs
    assert any("--assume-tz" in w for w in d.warnings)


def test_naive_vs_tz_with_assumption() -> None:
    d, pairs = diff_schemas(
        cols(("ts", "TIMESTAMP")),
        cols(("ts", "TIMESTAMP WITH TIME ZONE")),
        assume_tz="Europe/Berlin",
    )
    assert "ts" in pairs
    assert any("Europe/Berlin" in a for a in d.assumptions)


def test_timestamp_precision_assumption_recorded() -> None:
    d, pairs = diff_schemas(cols(("ts", "TIMESTAMP")), cols(("ts", "TIMESTAMP_MS")))
    assert "ts" in pairs
    assert any("milliseconds" in a and "--ts-precision" in a for a in d.assumptions)


def test_nullability_note() -> None:
    d, _pairs = diff_schemas(
        cols(("id", "BIGINT"), nullable=True),
        cols(("id", "BIGINT"), nullable=False),
    )
    assert "nullability" in d.columns[0].note


def test_order_changed_is_a_hint_not_an_error() -> None:
    d, pairs = diff_schemas(
        cols(("a", "BIGINT"), ("b", "VARCHAR")),
        cols(("b", "VARCHAR"), ("a", "BIGINT")),
    )
    assert d.order_changed
    assert not d.has_blocking
    assert len(pairs) == 2


def test_int_width_widening_reported() -> None:
    d, pairs = diff_schemas(cols(("n", "INTEGER")), cols(("n", "BIGINT")))
    assert d.columns[0].status == "widening"
    assert "n" in pairs


def test_string_vs_bool_alias_comparable() -> None:
    d, pairs = diff_schemas(cols(("flag", "VARCHAR")), cols(("flag", "BOOLEAN")))
    assert d.columns[0].status == "bool_alias"
    assert "flag" in pairs


def test_string_vs_date_castable() -> None:
    d, pairs = diff_schemas(cols(("d", "VARCHAR")), cols(("d", "DATE")))
    assert d.columns[0].status == "string_cast"
    assert "d" in pairs


def test_empty_right_side() -> None:
    d, pairs = diff_schemas(cols(("a", "BIGINT")), [])
    assert d.has_blocking
    assert pairs == {}
