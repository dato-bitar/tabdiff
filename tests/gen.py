"""Synthetic data generator with injected, known deviations.

This is the ground truth for every diff test: an injector returns the
mutated table plus a machine-readable description of what tabdiff MUST
report. If the generator says N deviations exist, a correct run finds
exactly those N - no more, no less.

Deterministic for a given seed.
"""

from __future__ import annotations

import json
import random
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import pyarrow as pa

NAMES = [
    "Alice",
    "Böb",  # precomposed umlaut
    "Çarol",
    "Dévid",
    "Eve",
    "Fränk",
    "Graça",
    "Håkon",
    "Ivo",
    "José",
    "Kåre",
    "Lütfi",
]

TAGS = ["alpha", "beta", "gamma", "delta"]


@dataclass
class Expected:
    """What a correct diff run must report for an injected deviation."""

    # cell-level differences keyed by column name (rows where they differ)
    cells: dict[str, int] = field(default_factory=dict)
    rows_only_left: int = 0
    rows_only_right: int = 0
    columns_only_left: list[str] = field(default_factory=list)
    columns_only_right: list[str] = field(default_factory=list)
    # schema-level notes that must appear (substring match)
    schema_notes: list[str] = field(default_factory=list)
    # the run must abort because the key is duplicated
    duplicate_key: bool = False
    # value comparison requires this --assume-tz zone
    require_assume_tz: str | None = None
    # assumptions/warnings substrings expected even on success
    assumptions: list[str] = field(default_factory=list)


@dataclass
class Injection:
    left: pa.Table
    right: pa.Table
    expected: Expected


def _payload(i: int, rng: random.Random) -> str:
    return json.dumps(
        {"n": i, "tag": rng.choice(TAGS), "meta": {"ok": True}}, separators=(",", ":")
    )


def make_base(n_rows: int = 500, seed: int = 42) -> pa.Table:
    """The canonical base table: mixed types, some NULLs, unicode names."""
    rng = random.Random(seed)
    ids = list(range(1, n_rows + 1))
    names: list[str | None] = []
    amounts: list[float | None] = []  # scaled decimals, stored via Decimal below
    scores: list[float | None] = []
    qtys: list[int | None] = []
    flags: list[bool | None] = []
    stamps: list[int | None] = []  # microseconds since epoch
    dates: list[int | None] = []  # days since epoch
    payloads: list[str | None] = []

    from datetime import datetime, timedelta
    from decimal import Decimal

    base_dt = datetime(2024, 1, 1, tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for i in ids:
        r = rng.random()
        names.append(None if r < 0.02 else rng.choice(NAMES) + f"_{i}")
        amounts.append(None if r < 0.04 else Decimal(f"{rng.uniform(-1000, 1000):.2f}"))
        scores.append(None if r < 0.03 else rng.gauss(100, 15))
        qtys.append(None if r < 0.05 else rng.randint(-50, 50))
        flags.append(None if r < 0.05 else rng.random() < 0.5)
        micros = rng.randrange(0, 3_600_000_000)
        dt = base_dt + timedelta(seconds=i) + timedelta(microseconds=micros)
        stamps.append((dt - epoch) // timedelta(microseconds=1))
        dates.append((base_dt + timedelta(days=i % 400)).date().toordinal() - 719163)
        payloads.append(_payload(i, rng))

    return pa.table(
        {
            "id": pa.array(ids, type=pa.int64()),
            "name": pa.array(names, type=pa.string()),
            "amount": pa.array(
                [None if a is None else a for a in amounts],
                type=pa.decimal128(18, 4),
            ),
            "score": pa.array(scores, type=pa.float64()),
            "qty": pa.array(qtys, type=pa.int32()),
            "flag": pa.array(flags, type=pa.bool_()),
            "created_at": pa.array(stamps, type=pa.timestamp("us")),
            "event_date": pa.array(dates, type=pa.date32()),
            "payload": pa.array(payloads, type=pa.string()),
        }
    )


# ---------------------------------------------------------------------------
# injectors: (left, right, expected); left is always the pristine base copy
# ---------------------------------------------------------------------------


def _copy(t: pa.Table) -> pa.Table:
    return t


def row_added(t: pa.Table, rng: random.Random, n: int = 3) -> Injection:
    ids = t.column("id").to_pylist()
    max_id = max(ids)
    extra_ids = [max_id + k + 1 for k in range(n)]
    sample = t.slice(rng.randrange(0, t.num_rows), 1).take(pa.array([0] * n, type=pa.int64()))
    sample = sample.set_column(
        sample.schema.get_field_index("id"), "id", pa.array(extra_ids, type=pa.int64())
    )
    b = pa.concat_tables([t, sample])
    return Injection(_copy(t), b, Expected(rows_only_right=n))


def row_deleted(t: pa.Table, rng: random.Random, n: int = 3) -> Injection:
    drop = set(rng.sample(range(t.num_rows), n))
    mask = [i not in drop for i in range(t.num_rows)]
    idx = pa.array([i for i, keep in enumerate(mask) if keep], type=pa.int64())
    b = t.take(idx)
    return Injection(_copy(t), b, Expected(rows_only_left=n))


def value_changed(t: pa.Table, rng: random.Random, n: int = 7) -> Injection:
    b = _copy(t)
    rows = rng.sample(range(t.num_rows), n)
    scores = b.column("score").to_pylist()
    for r in rows:
        scores[r] = (scores[r] or 0.0) + 17.5
    b = b.set_column(
        b.schema.get_field_index("score"), "score", pa.array(scores, type=pa.float64())
    )
    return Injection(_copy(t), b, Expected(cells={"score": n}))


def null_introduced(t: pa.Table, rng: random.Random, n: int = 5) -> Injection:
    b = _copy(t)
    rows = rng.sample(range(t.num_rows), n)
    names = b.column("name").to_pylist()
    for r in rows:
        names[r] = None
    b = b.set_column(b.schema.get_field_index("name"), "name", pa.array(names, type=pa.string()))
    return Injection(_copy(t), b, Expected(cells={"name": n}))


def type_widened(t: pa.Table, rng: random.Random) -> Injection:
    b = t.set_column(t.schema.get_field_index("qty"), "qty", t.column("qty").cast(pa.int64()))
    return Injection(
        _copy(t),
        b,
        Expected(schema_notes=["integer widths differ"]),
    )


def column_added(t: pa.Table, rng: random.Random) -> Injection:
    extra = pa.array([f"x{i}" for i in range(t.num_rows)], type=pa.string())
    b = t.append_column(pa.field("extra_note", pa.string()), extra)
    return Injection(_copy(t), b, Expected(columns_only_right=["extra_note"]))


def column_dropped(t: pa.Table, rng: random.Random) -> Injection:
    b = t.drop_columns(["payload"])
    return Injection(_copy(t), b, Expected(columns_only_left=["payload"]))


def column_renamed(t: pa.Table, rng: random.Random) -> Injection:
    b = t.rename_columns(["is_flag" if n == "flag" else n for n in t.schema.names])
    return Injection(
        _copy(t),
        b,
        Expected(columns_only_left=["flag"], columns_only_right=["is_flag"]),
    )


def precision_lost(t: pa.Table, rng: random.Random) -> Injection:
    # timestamps us -> ms (truncating), decimal 18,4 -> rounded to 18,2 so the
    # *values* stay identical and only representation precision changes.

    ts_ms = t.column("created_at").cast(pa.timestamp("ms"), safe=False)
    dec2 = t.column("amount").cast(pa.decimal128(18, 2))
    b = t
    b = b.set_column(b.schema.get_field_index("created_at"), "created_at", ts_ms)
    b = b.set_column(b.schema.get_field_index("amount"), "amount", dec2)
    # coarse comparison must see NO value difference, only schema notes.
    return Injection(
        _copy(t),
        b,
        Expected(schema_notes=["precision"], assumptions=["milliseconds"]),
    )


def timezone_shifted(t: pa.Table, rng: random.Random) -> Injection:
    """Right side stores TIMESTAMPTZ(UTC); naive side is UTC instants.

    With --assume-tz=UTC the instants match (0 diffs). With the *wrong*
    assumption (e.g. Europe/Berlin) every row must differ - that is how a
    wrong zone assumption is detected.
    """
    aware = pa.compute.cast(t.column("created_at"), pa.timestamp("us", tz="UTC"))
    b = t.set_column(t.schema.get_field_index("created_at"), "created_at", aware)
    return Injection(
        _copy(t),
        b,
        Expected(schema_notes=["TIME ZONE"]),
    )


def encoding_mangled(t: pa.Table, rng: random.Random, n: int = 6) -> Injection:
    """Mojibake: UTF-8 bytes decoded as latin-1 on selected rows."""
    b = _copy(t)
    rows = [
        i
        for i, v in enumerate(b.column("name").to_pylist())
        if v is not None and any(ord(ch) > 127 for ch in v)
    ]
    rng.shuffle(rows)
    rows = rows[:n]
    names = b.column("name").to_pylist()
    for r in rows:
        raw = names[r].encode("utf-8")
        names[r] = raw.decode("latin-1")
    b = b.set_column(b.schema.get_field_index("name"), "name", pa.array(names, type=pa.string()))
    return Injection(_copy(t), b, Expected(cells={"name": len(rows)}))


def nfc_equivalent(t: pa.Table, rng: random.Random) -> Injection:
    """NOT a deviation: NFD-decomposed names must produce ZERO diffs (NFC)."""
    b = _copy(t)
    names = b.column("name").to_pylist()
    names = [unicodedata.normalize("NFD", v) if v is not None else None for v in names]
    b = b.set_column(b.schema.get_field_index("name"), "name", pa.array(names, type=pa.string()))
    return Injection(_copy(t), b, Expected())


def duplicate_key_introduced(t: pa.Table, rng: random.Random) -> Injection:
    b = _copy(t)
    src_row = rng.randrange(0, t.num_rows)
    dup = t.slice(src_row, 1)
    b = pa.concat_tables([b, dup])
    return Injection(_copy(t), b, Expected(duplicate_key=True))


def order_shuffled(t: pa.Table, rng: random.Random) -> Injection:
    perm = list(range(t.num_rows))
    rng.shuffle(perm)
    b = t.take(pa.array(perm, type=pa.int64()))
    return Injection(_copy(t), b, Expected())


def column_name_case_space(t: pa.Table, rng: random.Random) -> Injection:
    """Left 'User ID' vs right 'user_id': same data, incompatible names.

    Must be reported as only_left/only_right - never silently matched.
    """
    vals = [None if i % 9 == 0 else f"user_{i}" for i in range(t.num_rows)]
    arr = pa.array(vals, type=pa.string())
    left = t.append_column(pa.field("User ID", pa.string()), arr)
    right = t.append_column(pa.field("user_id", pa.string()), arr)
    return Injection(
        left,
        right,
        Expected(columns_only_left=["User ID"], columns_only_right=["user_id"]),
    )


def special_char_column_names(t: pa.Table, rng: random.Random) -> Injection:
    """Quotes/semicolons/spaces inside one column name; identical values.

    Must round-trip through SQL quoting with ZERO differences on both
    strategies.
    """
    col = 'we"ird ;col'
    arr = pa.array([f"v{i}" for i in range(t.num_rows)], type=pa.string())
    left = t.append_column(pa.field(col, pa.string()), arr)
    right = t.append_column(pa.field(col, pa.string()), arr)
    return Injection(left, right, Expected())


def case_colliding_columns(t: pa.Table, rng: random.Random) -> Injection:
    """'Delta' and 'delta' are DIFFERENT columns and must stay separate.

    Three rows change in 'Delta' only; a diff that case-folded names would
    misattribute them (or merge the columns). Note: DuckDB renames the second
    case-folded duplicate to 'delta_1' on read - identically on both sides,
    so the diff stays correct under the effective name.
    """
    n = t.num_rows
    rows = set(rng.sample(range(n), min(3, n)))
    hi_l = pa.array([i * 10 for i in range(n)], type=pa.int64())
    lo = pa.array([-(i * 10) for i in range(n)], type=pa.int64())
    hi_r = pa.array([i * 10 + (1 if i in rows else 0) for i in range(n)], type=pa.int64())
    left = t.append_column(pa.field("Delta", pa.int64()), hi_l).append_column(
        pa.field("delta", pa.int64()), lo
    )
    right = t.append_column(pa.field("Delta", pa.int64()), hi_r).append_column(
        pa.field("delta", pa.int64()), lo
    )
    return Injection(left, right, Expected(cells={"Delta": len(rows)}))


def long_strings(t: pa.Table, rng: random.Random) -> Injection:
    """~10 KB text per cell; a handful of near-identical giant cells differ."""
    n = t.num_rows
    chunk = "abcdefghijklmnopqrstuvwxyz0123456789" * 300  # >10 KB
    changed = set(rng.sample(range(n), min(4, n)))
    left_texts = [chunk[:10190] + f"|{i:04d}|" for i in range(n)]
    right_texts = [
        chunk[:10190] + f"!{i:04d}!" if i in changed else left_texts[i] for i in range(n)
    ]
    left = t.append_column(pa.field("long_text", pa.string()), pa.array(left_texts))
    right = t.append_column(pa.field("long_text", pa.string()), pa.array(right_texts))
    return Injection(left, right, Expected(cells={"long_text": len(changed)}))


def mostly_nulls(t: pa.Table, rng: random.Random) -> Injection:
    """A ~99% NULL column; changes to its rare non-nulls must be found."""
    n = t.num_rows
    nn_count = max(3, n // 100)
    step = max(1, n // nn_count)
    idxs = list(range(0, n, step))[:nn_count]
    left_sparse: list[str | None] = [None] * n
    for j, i in enumerate(idxs):
        left_sparse[i] = f"s{j}"
    changed = idxs[: min(3, len(idxs))]
    right_sparse = list(left_sparse)
    for j, i in enumerate(changed):
        right_sparse[i] = f"s{j}-changed"
    left = t.append_column(pa.field("sparse", pa.string()), pa.array(left_sparse))
    right = t.append_column(pa.field("sparse", pa.string()), pa.array(right_sparse))
    return Injection(left, right, Expected(cells={"sparse": len(changed)}))


INJECTORS: dict[str, Any] = {
    "row_added": row_added,
    "row_deleted": row_deleted,
    "value_changed": value_changed,
    "null_introduced": null_introduced,
    "type_widened": type_widened,
    "column_added": column_added,
    "column_dropped": column_dropped,
    "column_renamed": column_renamed,
    "precision_lost": precision_lost,
    "timezone_shifted": timezone_shifted,
    "encoding_mangled": encoding_mangled,
    "duplicate_key_introduced": duplicate_key_introduced,
    "order_shuffled": order_shuffled,
    "column_name_case_space": column_name_case_space,
    "special_char_column_names": special_char_column_names,
    "case_colliding_columns": case_colliding_columns,
    "long_strings": long_strings,
    "mostly_nulls": mostly_nulls,
}

ALL_INJECTIONS = sorted(INJECTORS)


def build(kind: str, *, n_rows: int = 500, seed: int = 42) -> Injection:
    """Build (left, right, expected) for one injection kind."""
    if kind not in INJECTORS:
        msg = f"unknown injection kind {kind!r}; have {ALL_INJECTIONS}"
        raise ValueError(msg)
    rng = random.Random(seed + 1)
    base = make_base(n_rows=n_rows, seed=seed)
    return INJECTORS[kind](base, rng)


def write_parquet_pair(
    kind: str, dir_path: str, *, n_rows: int = 500, seed: int = 42
) -> tuple[str, str, Expected]:
    import pathlib

    d = pathlib.Path(dir_path)
    d.mkdir(parents=True, exist_ok=True)
    inj = build(kind, n_rows=n_rows, seed=seed)
    lp, rp = d / f"{kind}_left.parquet", d / f"{kind}_right.parquet"
    import pyarrow.parquet as pq

    pq.write_table(inj.left, lp)
    pq.write_table(inj.right, rp)
    return str(lp), str(rp), inj.expected


def demo() -> None:  # pragma: no cover - manual smoke target
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        lp, rp, _exp = write_parquet_pair("value_changed", td, n_rows=200)
        subprocess.run([sys.executable, "-m", "tabdiff.cli", "diff", lp, rp], check=False)


if __name__ == "__main__":  # pragma: no cover
    demo()
