"""Unit tests for the canonical type lattice and pairwise classification."""

from __future__ import annotations

import pytest

from tabdiff.normalize import Canon, Verdict, classify, parse_type


class TestParseType:
    @pytest.mark.parametrize(
        ("raw", "canon"),
        [
            ("BIGINT", Canon.INTEGER),
            ("INTEGER", Canon.INTEGER),
            ("SMALLINT", Canon.INTEGER),
            ("HUGEINT", Canon.INTEGER),
            ("DOUBLE", Canon.FLOAT),
            ("FLOAT", Canon.FLOAT),
            ("REAL", Canon.FLOAT),
            ("VARCHAR", Canon.STRING),
            ("TEXT", Canon.STRING),
            ("BOOLEAN", Canon.BOOLEAN),
            ("DATE", Canon.DATE),
            ("TIME", Canon.TIME),
            ("BLOB", Canon.BINARY),
            ("JSON", Canon.JSON),
            ("UUID", Canon.UUID),
            ("DECIMAL(18,3)", Canon.DECIMAL),
            ("NUMERIC", Canon.DECIMAL),
        ],
    )
    def test_basic_mapping(self, raw: str, canon: Canon) -> None:
        assert parse_type(raw).canon is canon

    def test_timestamp_naive(self) -> None:
        ti = parse_type("TIMESTAMP")
        assert ti.canon is Canon.TIMESTAMP_NAIVE
        assert ti.precision == 6

    def test_timestamp_tz(self) -> None:
        ti = parse_type("TIMESTAMP WITH TIME ZONE")
        assert ti.canon is Canon.TIMESTAMP_TZ

    def test_timestamp_units(self) -> None:
        assert parse_type("TIMESTAMP_S").precision == 0
        assert parse_type("TIMESTAMP_MS").precision == 3
        assert parse_type("TIMESTAMP_NS").precision == 9

    def test_decimal_scale(self) -> None:
        assert parse_type("DECIMAL(18,4)").precision == 4

    def test_unknown_is_other(self) -> None:
        assert parse_type("SOMETHING_ODD(3)").canon is Canon.OTHER

    def test_nullable_passthrough(self) -> None:
        assert parse_type("BIGINT", nullable=False).nullable is False


def _t(raw: str) -> object:
    return parse_type(raw)


class TestClassify:
    def test_identical(self) -> None:
        pc = classify(_t("BIGINT"), _t("BIGINT"))  # type: ignore[arg-type]
        assert pc.verdict is Verdict.SAME

    def test_int_widths_widen(self) -> None:
        pc = classify(_t("INTEGER"), _t("BIGINT"))  # type: ignore[arg-type]
        assert pc.verdict is Verdict.WIDENING

    def test_decimal_scales_benign(self) -> None:
        pc = classify(_t("DECIMAL(18,2)"), _t("DECIMAL(18,4)"))  # type: ignore[arg-type]
        assert pc.verdict is Verdict.BENIGN

    def test_decimal_vs_float_lossy(self) -> None:
        pc = classify(_t("DECIMAL(18,2)"), _t("DOUBLE"))  # type: ignore[arg-type]
        assert pc.verdict is Verdict.LOSSY
        assert "float" in pc.note.lower()

    def test_float_vs_int_lossy(self) -> None:
        assert classify(_t("FLOAT"), _t("INTEGER")).verdict is Verdict.LOSSY

    def test_ts_naive_vs_tz_needs_assumption(self) -> None:
        pc = classify(_t("TIMESTAMP"), _t("TIMESTAMP WITH TIME ZONE"))  # type: ignore[arg-type]
        assert pc.verdict is Verdict.NEEDS_TZ

    def test_string_vs_int_string_cast(self) -> None:
        assert classify(_t("VARCHAR"), _t("BIGINT")).verdict is Verdict.STRING_CAST

    def test_string_vs_date_string_cast(self) -> None:
        assert classify(_t("VARCHAR"), _t("DATE")).verdict is Verdict.STRING_CAST

    def test_string_vs_bool_alias(self) -> None:
        assert classify(_t("VARCHAR"), _t("BOOLEAN")).verdict is Verdict.BOOL_ALIAS

    def test_string_vs_blob_incompatible(self) -> None:
        assert classify(_t("VARCHAR"), _t("BLOB")).verdict is Verdict.INCOMPATIBLE

    def test_blob_vs_int_incompatible(self) -> None:
        assert classify(_t("BLOB"), _t("BIGINT")).verdict is Verdict.INCOMPATIBLE

    def test_other_incompatible(self) -> None:
        assert classify(_t("SOMETHING"), _t("BIGINT")).verdict is Verdict.INCOMPATIBLE

    def test_precision_difference_benign(self) -> None:
        pc = classify(_t("TIMESTAMP_MS"), _t("TIMESTAMP_NS"))  # type: ignore[arg-type]
        assert pc.verdict is Verdict.BENIGN
        assert "precision" in pc.note

    def test_uuid_spelling_benign(self) -> None:
        assert classify(parse_type("UUID"), parse_type("UUID")).verdict is Verdict.SAME
