"""Generator determinism and per-injection shape tests."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from tests.gen import ALL_INJECTIONS, build, make_base


class TestDeterminism:
    def test_base_table_reproducible(self) -> None:
        a = make_base(n_rows=200, seed=7)
        b = make_base(n_rows=200, seed=7)
        assert a.equals(b)

    def test_base_table_seed_sensitive(self) -> None:
        a = make_base(n_rows=200, seed=7)
        b = make_base(n_rows=200, seed=8)
        assert not a.equals(b)

    @pytest.mark.parametrize("kind", ALL_INJECTIONS)
    def test_injections_reproducible(self, kind: str) -> None:
        i1 = build(kind, n_rows=150, seed=5)
        i2 = build(kind, n_rows=150, seed=5)
        assert i1.left.equals(i2.left)
        assert i1.right.equals(i2.right)


class TestBaseShape:
    def test_columns_present(self) -> None:
        names = set(make_base(n_rows=10).schema.names)
        assert {
            "id",
            "name",
            "amount",
            "score",
            "qty",
            "flag",
            "created_at",
            "event_date",
            "payload",
        } <= names

    def test_key_unique(self) -> None:
        ids = make_base(n_rows=300).column("id").to_pylist()
        assert len(ids) == len(set(ids))

    def test_has_nulls_and_unicode(self) -> None:
        t = make_base(n_rows=400)
        assert pc.sum(pc.is_null(t.column("name")).cast(pa.int64())).as_py() > 0
        assert any(v and any(ord(c) > 127 for c in v) for v in t.column("name").to_pylist())


class TestInjectionShapes:
    def test_row_added(self) -> None:
        inj = build("row_added", n_rows=100)
        assert inj.right.num_rows == inj.left.num_rows + inj.expected.rows_only_right
        left_ids = set(inj.left.column("id").to_pylist())
        right_ids = set(inj.right.column("id").to_pylist())
        assert right_ids - left_ids and len(right_ids - left_ids) == 3

    def test_row_deleted(self) -> None:
        inj = build("row_deleted", n_rows=100)
        assert inj.right.num_rows == inj.left.num_rows - 3

    def test_value_changed_exactly_n_cells(self) -> None:
        inj = build("value_changed", n_rows=100)
        left_tbl = inj.left.column("score").to_pylist()
        r = inj.right.column("score").to_pylist()
        diffs = sum(1 for a, b in zip(left_tbl, r, strict=True) if a != b)
        assert diffs == inj.expected.cells["score"]
        assert inj.left.schema.equals(inj.right.schema)

    def test_null_introduced(self) -> None:
        inj = build("null_introduced", n_rows=100)
        left_tbl = inj.left.column("name").to_pylist()
        r = inj.right.column("name").to_pylist()
        new_nulls = sum(1 for a, b in zip(left_tbl, r, strict=True) if a is not None and b is None)
        assert new_nulls == inj.expected.cells["name"]

    def test_type_widened(self) -> None:
        inj = build("type_widened", n_rows=50)
        assert inj.left.schema.field("qty").type == pa.int32()
        assert inj.right.schema.field("qty").type == pa.int64()

    def test_column_added_dropped_renamed(self) -> None:
        added = build("column_added", n_rows=20)
        assert "extra_note" in added.right.schema.names
        dropped = build("column_dropped", n_rows=20)
        assert "payload" not in dropped.right.schema.names
        renamed = build("column_renamed", n_rows=20)
        assert "flag" not in renamed.right.schema.names
        assert "is_flag" in renamed.right.schema.names

    def test_precision_lost_keeps_values_roundable(self) -> None:
        inj = build("precision_lost", n_rows=60)
        assert inj.right.schema.field("created_at").type == pa.timestamp("ms")
        # ms cast truncates; coarse comparison later treats both at ms
        assert inj.left.schema.field("amount").type == pa.decimal128(18, 4)

    def test_encoding_mangled_is_real_difference(self) -> None:
        inj = build("encoding_mangled", n_rows=80)
        left_tbl = inj.left.column("name").to_pylist()
        r = inj.right.column("name").to_pylist()
        changed = sum(1 for a, b in zip(left_tbl, r, strict=True) if a != b)
        assert changed >= 1

    def test_duplicate_key_introduced(self) -> None:
        inj = build("duplicate_key_introduced", n_rows=50)
        ids = inj.right.column("id").to_pylist()
        assert len(ids) != len(set(ids))

    def test_order_shuffled_same_multiset(self) -> None:
        inj = build("order_shuffled", n_rows=120)
        assert sorted(inj.left.column("id").to_pylist()) == sorted(
            inj.right.column("id").to_pylist()
        )
        assert inj.expected.cells == {}

    def test_timezone_shifted_schema(self) -> None:
        inj = build("timezone_shifted", n_rows=30)
        assert inj.right.schema.field("created_at").type.tz is not None

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown injection"):
            build("no_such_kind")
