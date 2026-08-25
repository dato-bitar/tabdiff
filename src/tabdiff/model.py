"""Data model for diff results - stable, versioned output contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tabdiff.schema_diff import SchemaDiff

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CellExample:
    key: str
    left: str | None
    right: str | None


@dataclass
class ColumnValueDiff:
    column: str
    mismatched_rows: int
    examples: list[CellExample] = field(default_factory=list)
    approximated: bool = False  # true when JSON refinement hit its cap


@dataclass
class ValueDiffResult:
    changed_rows: int = 0
    columns: list[ColumnValueDiff] = field(default_factory=list)

    @property
    def total_cell_mismatches(self) -> int:
        return sum(c.mismatched_rows for c in self.columns)


@dataclass
class CountsDiff:
    left_total: int = 0
    right_total: int = 0
    left_only: int = 0
    right_only: int = 0
    both: int = 0


@dataclass
class ColumnStats:
    min_: str | None = None
    max_: str | None = None
    avg: str | None = None
    null_count: int | None = None
    distinct_count: int | None = None


@dataclass
class StatsDrift:
    """Per-column statistics, left vs right (informational)."""

    columns: dict[str, tuple[ColumnStats, ColumnStats]] = field(default_factory=dict)


@dataclass
class DiffMeta:
    strategy: str  # join | hash | keyless
    key: list[str]
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    # side -> execution path, e.g. {"left": "pushdown", "right": "local-scan"};
    # empty for strategies that have no per-side path choice
    execution_path: dict[str, str] = field(default_factory=dict)


@dataclass
class DiffReport:
    meta: DiffMeta
    schema: SchemaDiff | None = None
    counts: CountsDiff | None = None
    values: ValueDiffResult | None = None
    stats: StatsDrift | None = None

    @property
    def identical(self) -> bool:
        schema_ok = self.schema is None or self.schema.identical
        counts_ok = self.counts is None or (
            self.counts.left_only == 0
            and self.counts.right_only == 0
            and self.counts.left_total == self.counts.right_total
        )
        values_ok = self.values is None or self.values.total_cell_mismatches == 0
        return schema_ok and counts_ok and values_ok

    @property
    def has_differences(self) -> bool:
        return not self.identical
