"""Source abstraction: anything tabular that tabdiff can bind into a session."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tabdiff.session import quote_ident

if TYPE_CHECKING:
    import pyarrow as pa

    from tabdiff.session import Session


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str  # DuckDB type string, e.g. BIGINT, VARCHAR, TIMESTAMP WITH TIME ZONE
    nullable: bool = True


class BoundSource(ABC):
    """A concrete source bound under an alias inside a :class:`Session`."""

    def __init__(self, session: Session, alias: str) -> None:
        self.session = session
        self.alias = alias

    @abstractmethod
    def relation_sql(self, *, columns: list[str] | None = None) -> str:
        """SQL fragment selecting (optionally projected) rows from this source."""

    @abstractmethod
    def declared_columns(self) -> list[ColumnInfo]:
        """Columns as declared by the source's own catalog/metadata."""

    @property
    def engine(self) -> str:
        """Which engine ultimately computes aggregates for this source."""
        return "duckdb"

    # -- shared implementations --------------------------------------------

    def columns(self) -> list[ColumnInfo]:
        """Effective columns as seen by DuckDB (the comparison lingua franca)."""
        rel = self.relation_sql()
        rows = self.session.rows(f"DESCRIBE {rel}")
        out: list[ColumnInfo] = []
        for name, typ, nullable, *_rest in rows:
            out.append(
                ColumnInfo(
                    name=str(name),
                    type=str(typ),
                    nullable=str(nullable).upper() != "NO",
                )
            )
        return out

    def count(self) -> int:
        return int(self.session.scalar(f"SELECT count(*) FROM ({self.relation_sql()})"))

    def arrow_schema(self) -> pa.Schema:
        rel = self.relation_sql()
        return self.session.arrow(f"SELECT * FROM ({rel}) LIMIT 0").schema

    def primary_key_hint(self) -> list[str]:
        """Best-effort primary key columns; empty when unknown."""
        return []

    def qualified_table_for_catalog(self) -> tuple[str, str]:
        """(schema, table) for catalog lookups; ('', alias) when not applicable."""
        return "", self.alias


class RelationSource(BoundSource):
    """Base for sources exposed as a plain relation (view or attached table)."""

    def __init__(
        self,
        session: Session,
        alias: str,
        qualified_name: str,
        declared: list[ColumnInfo] | None = None,
    ) -> None:
        super().__init__(session, alias)
        self.qualified_name = qualified_name
        self.declared_override = declared

    def relation_sql(self, *, columns: list[str] | None = None) -> str:
        base = self.qualified_name
        if columns is None:
            return f"(SELECT * FROM {base})"
        proj = ", ".join(quote_ident(c) for c in columns)
        return f"(SELECT {proj} FROM {base})"

    def declared_columns(self) -> list[ColumnInfo]:
        return self.declared_override if self.declared_override is not None else self.columns()


def describe_to_columns(rows: list[tuple[Any, ...]]) -> list[ColumnInfo]:
    return [
        ColumnInfo(name=str(r[0]), type=str(r[1]), nullable=str(r[2]).upper() != "NO") for r in rows
    ]
