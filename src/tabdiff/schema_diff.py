"""Schema-level comparison of two column lists."""

from __future__ import annotations

from dataclasses import dataclass, field

from tabdiff.canon import CompareOptions, effective_precision
from tabdiff.normalize import PairClass, TypeInfo, classify, comparable
from tabdiff.source.base import ColumnInfo


@dataclass(frozen=True)
class ColumnDiff:
    name: str
    # only_left | only_right | same | benign | widening | lossy |
    # needs_tz | string_cast | bool_alias | incompatible
    status: str
    left_type: str | None = None
    right_type: str | None = None
    note: str = ""


@dataclass
class SchemaDiff:
    columns: list[ColumnDiff] = field(default_factory=list)
    order_changed: bool = False
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_status(self, *statuses: str) -> list[ColumnDiff]:
        return [c for c in self.columns if c.status in statuses]

    @property
    def has_blocking(self) -> bool:
        """True when a column is missing on one side (keys may be unusable)."""
        return bool(self.by_status("only_left", "only_right"))

    @property
    def identical(self) -> bool:
        common = [c for c in self.columns if c.status not in {"only_left", "only_right"}]
        only = self.by_status("only_left", "only_right")
        return not only and all(c.status == "same" and not c.note for c in common)


def _nullability_note(left: ColumnInfo, right: ColumnInfo) -> str:
    if left.nullable != right.nullable:
        ln = "nullable" if left.nullable else "NOT NULL"
        rn = "nullable" if right.nullable else "NOT NULL"
        return f" nullability differs ({ln} vs {rn})"
    return ""


def diff_schemas(
    l_cols: list[ColumnInfo],
    r_cols: list[ColumnInfo],
    *,
    assume_tz: str | None = None,
    ts_precision: str = "coarse",
) -> tuple[SchemaDiff, dict[str, tuple[TypeInfo, TypeInfo, PairClass]]]:
    """Compare two column lists.

    Returns the reportable diff plus the comparable column pairs
    (name -> (left TypeInfo, right TypeInfo, classification)) that the value
    diff should process.
    """
    d = SchemaDiff()
    l_by = {c.name: c for c in l_cols}
    r_by = {c.name: c for c in r_cols}

    common_l = [c.name for c in l_cols if c.name in r_by]
    common_r = [c.name for c in r_cols if c.name in l_by]
    d.order_changed = common_l != common_r

    opts = CompareOptions(assume_tz=assume_tz, ts_precision=ts_precision)
    comparable_pairs: dict[str, tuple[TypeInfo, TypeInfo, PairClass]] = {}

    for col in l_cols:
        if col.name not in r_by:
            d.columns.append(ColumnDiff(col.name, "only_left", left_type=col.type))
            continue
        rc = r_by[col.name]
        lt = parse_type(col)
        rt = parse_type(rc)
        pc = classify(lt, rt)
        note = pc.note + _nullability_note(col, rc)
        status = "same" if pc.verdict.value == "same" and not note else pc.verdict.value
        d.columns.append(ColumnDiff(col.name, status, col.type, rc.type, note))

        if pc.verdict.value == "needs_tz":
            if assume_tz:
                d.assumptions.append(
                    f"column '{col.name}': naive timestamps interpreted as {assume_tz} "
                    "(--assume-tz), compared in UTC"
                )
                comparable_pairs[col.name] = (lt, rt, pc)
            else:
                d.warnings.append(
                    f"column '{col.name}': TIMESTAMP WITH/WITHOUT TIME ZONE mismatch - "
                    "values NOT compared; pass --assume-tz to enable"
                )
        elif comparable(pc.verdict):
            comparable_pairs[col.name] = (lt, rt, pc)
            if pc.verdict.value == "lossy":
                d.warnings.append(f"column '{col.name}': {note} - values compared, see note")
        else:
            d.warnings.append(
                f"column '{col.name}': {note or 'types incompatible'} - values NOT compared"
            )

    for col in r_cols:
        if col.name not in l_by:
            d.columns.append(ColumnDiff(col.name, "only_right", right_type=col.type))

    # stash effective timestamp precision per pair on the classification side
    for name, (lt, rt, _pc) in comparable_pairs.items():
        prec = effective_precision(lt, rt, opts)
        if prec is not None and any(t.precision != prec for t in (lt, rt) if t.is_temporal):
            coarse_name = {0: "seconds", 3: "milliseconds", 6: "microseconds", 9: "nanoseconds"}[
                min(prec, 9)
            ]
            if opts.ts_precision == "coarse":
                d.assumptions.append(
                    f"column '{name}': both sides truncated to {coarse_name} precision "
                    "(coarser of the two; control with --ts-precision)"
                )

    return d, comparable_pairs


def parse_type(col: ColumnInfo) -> TypeInfo:
    from tabdiff.normalize import parse_type as _p  # noqa: PLC0415

    return _p(col.type, nullable=col.nullable)


__all__ = ["ColumnDiff", "SchemaDiff", "diff_schemas"]
