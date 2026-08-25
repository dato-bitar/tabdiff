"""Canonical type lattice and pairwise classification.

Every source speaks DuckDB type strings (see DECISIONS.md §4); this module
maps them onto a small lattice and decides whether/how two columns can be
compared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto


class Canon(Enum):
    BOOLEAN = auto()
    INTEGER = auto()
    DECIMAL = auto()
    FLOAT = auto()
    STRING = auto()
    BINARY = auto()
    DATE = auto()
    TIME = auto()
    TIMESTAMP_NAIVE = auto()
    TIMESTAMP_TZ = auto()
    JSON = auto()
    UUID = auto()
    OTHER = auto()


_INT_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
    "INT",
    "INT2",
    "INT4",
    "INT8",
    "SHORT",
    "LONG",
}
_FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL", "FLOAT4", "FLOAT8"}
_STRING_TYPES = {"VARCHAR", "CHAR", "BPCHAR", "TEXT", "STRING", "CHARACTER VARYING"}

_DECIMAL_RE = re.compile(r"^DECIMAL\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)$")
_TS_RE = re.compile(r"^TIMESTAMP(_S|_MS|_NS)?(\s+WITH\s+TIME\s+ZONE)?$")


@dataclass(frozen=True)
class TypeInfo:
    """Normalized view of one column type."""

    canon: Canon
    duckdb_type: str
    precision: int = 0  # timestamp sub-second digits (6 = microseconds); decimal scale
    nullable: bool = True

    @property
    def is_numeric(self) -> bool:
        return self.canon in {Canon.INTEGER, Canon.DECIMAL, Canon.FLOAT}

    @property
    def is_temporal(self) -> bool:
        return self.canon in {
            Canon.DATE,
            Canon.TIME,
            Canon.TIMESTAMP_NAIVE,
            Canon.TIMESTAMP_TZ,
        }


def parse_type(type_str: str, *, nullable: bool = True) -> TypeInfo:
    t = re.sub(r"\s+", " ", type_str.strip().upper())
    if t in {"BOOLEAN", "BOOL"}:
        return TypeInfo(Canon.BOOLEAN, type_str, nullable=nullable)
    if t in _INT_TYPES:
        return TypeInfo(Canon.INTEGER, type_str, nullable=nullable)
    if t in _FLOAT_TYPES:
        return TypeInfo(Canon.FLOAT, type_str, nullable=nullable)
    m = _DECIMAL_RE.match(t)
    if m:
        return TypeInfo(Canon.DECIMAL, type_str, precision=int(m.group(2)), nullable=nullable)
    if t == "NUMERIC":
        return TypeInfo(Canon.DECIMAL, type_str, nullable=nullable)
    m = _TS_RE.match(t)
    if m:
        frac = m.group(1) or ""
        prec = {"": 6, "_S": 0, "_MS": 3, "_NS": 9}[frac]
        tz = bool(m.group(2))
        return TypeInfo(
            Canon.TIMESTAMP_TZ if tz else Canon.TIMESTAMP_NAIVE,
            type_str,
            precision=prec,
            nullable=nullable,
        )
    if t in _STRING_TYPES:
        return TypeInfo(Canon.STRING, type_str, nullable=nullable)
    if t in {"BLOB", "BYTEA", "BINARY", "VARBINARY"}:
        return TypeInfo(Canon.BINARY, type_str, nullable=nullable)
    if t == "DATE":
        return TypeInfo(Canon.DATE, type_str, nullable=nullable)
    if t.startswith("TIME"):
        return TypeInfo(Canon.TIME, type_str, nullable=nullable)
    if t in {"JSON", "JSONB"}:
        return TypeInfo(Canon.JSON, type_str, nullable=nullable)
    if t == "UUID":
        return TypeInfo(Canon.UUID, type_str, nullable=nullable)
    return TypeInfo(Canon.OTHER, type_str, nullable=nullable)


class Verdict(Enum):
    """Outcome of classifying two column types against each other."""

    SAME = "same"
    BENIGN = "benign"  # differences exist but comparison is exact and safe
    WIDENING = "widening"  # representable difference, comparison safe, reported
    LOSSY = "lossy"  # comparison goes through a lossy representation (reported)
    NEEDS_TZ = "needs_tz"  # naive vs tz timestamp; needs --assume-tz
    STRING_CAST = "string_cast"  # one side is string; values must parse
    BOOL_ALIAS = "bool_alias"  # string side holds boolean aliases
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class PairClass:
    verdict: Verdict
    note: str


def classify(left: TypeInfo, right: TypeInfo) -> PairClass:
    lt, rt = left.canon, right.canon
    if (
        left.duckdb_type.upper() == right.duckdb_type.upper()
        and (lt is Canon.TIMESTAMP_NAIVE or lt is Canon.TIMESTAMP_TZ)
        and left.precision != right.precision
    ):
        return PairClass(
            Verdict.BENIGN,
            f"timestamp precision differs ({left.precision} vs {right.precision} "
            "sub-second digits)",
        )
    if left.duckdb_type.upper() == right.duckdb_type.upper():
        return PairClass(Verdict.SAME, "")
    if lt == rt:
        if lt is Canon.TIMESTAMP_NAIVE or lt is Canon.TIMESTAMP_TZ:
            return PairClass(
                Verdict.BENIGN, f"timestamp precision ({left.precision} vs {right.precision})"
            )
        if lt is Canon.INTEGER:
            return PairClass(Verdict.WIDENING, f"integer widths differ ({left}/{right})")
        if lt is Canon.DECIMAL:
            return PairClass(
                Verdict.BENIGN, f"decimal scales differ ({left.duckdb_type}/{right.duckdb_type})"
            )
        if lt is Canon.STRING or lt is Canon.BOOLEAN or lt is Canon.DATE:
            return PairClass(Verdict.SAME, "")
        return PairClass(Verdict.BENIGN, f"same family, different spelling ({left}/{right})")

    # mixed families ---------------------------------------------------------
    num = {Canon.INTEGER, Canon.DECIMAL, Canon.FLOAT}
    if lt in num and rt in num:
        if Canon.FLOAT in (lt, rt):
            return PairClass(
                Verdict.LOSSY,
                "float participates; comparison happens in double precision - "
                "use tolerances for cross-engine float data",
            )
        return PairClass(
            Verdict.WIDENING, f"exact numeric families differ ({left.duckdb_type}/{right})"
        )

    if {lt, rt} == {Canon.TIMESTAMP_NAIVE, Canon.TIMESTAMP_TZ}:
        return PairClass(
            Verdict.NEEDS_TZ,
            "TIMESTAMP WITHOUT TIME ZONE vs TIMESTAMP WITH TIME ZONE: pass --assume-tz to "
            "compare values; the naive side is interpreted in the given zone",
        )

    if lt is Canon.STRING and rt is not Canon.STRING:
        return _string_vs(right)
    if rt is Canon.STRING and lt is not Canon.STRING:
        return _string_vs(left)

    if {lt, rt} == {Canon.JSON, Canon.STRING}:
        return PairClass(Verdict.STRING_CAST, "json stored as text on one side")

    return PairClass(
        Verdict.INCOMPATIBLE, f"no comparison rule for {left.duckdb_type} vs {right.duckdb_type}"
    )


def _string_vs(other: TypeInfo) -> PairClass:
    if other.canon is Canon.BOOLEAN:
        return PairClass(Verdict.BOOL_ALIAS, "boolean stored as text on one side")
    if other.is_numeric or other.canon in {
        Canon.DATE,
        Canon.TIME,
        Canon.TIMESTAMP_NAIVE,
        Canon.TIMESTAMP_TZ,
        Canon.UUID,
        Canon.JSON,
    }:
        return PairClass(Verdict.STRING_CAST, f"text on one side, {other.duckdb_type} on the other")
    return PairClass(Verdict.INCOMPATIBLE, f"string vs {other.duckdb_type}")


def comparable(verdict: Verdict) -> bool:
    return verdict is not Verdict.INCOMPATIBLE and verdict is not Verdict.NEEDS_TZ
