"""File-based sources: Parquet and CSV."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from tabdiff.errors import SourceError
from tabdiff.session import quote_ident, quote_literal
from tabdiff.source.base import BoundSource, ColumnInfo, RelationSource

if TYPE_CHECKING:
    from tabdiff.session import Session


def _is_timestamp_type(t: object) -> bool:
    return bool(pa.types.is_timestamp(t))


class ParquetSource(RelationSource):
    def __init__(self, session: Session, alias: str, path: Path) -> None:
        if not path.is_file():
            msg = f"parquet file not found: {path}"
            raise SourceError(msg)
        view_sql = f"SELECT * FROM read_parquet({quote_literal(str(path))})"
        session.create_view(alias, view_sql)
        super().__init__(session, alias, quote_ident(alias))
        self.path = path

    def columns(self) -> list[ColumnInfo]:
        """DuckDB types refined with the Arrow schema's true time units.

        DuckDB normalizes every parquet timestamp to microseconds, silently
        losing s/ms/ns units - exactly what precision-aware comparison needs.
        """
        cols = super().columns()
        schema = pq.read_schema(self.path)
        out: list[ColumnInfo] = []
        for c in cols:
            try:
                idx = schema.get_field_index(c.name)
                f = schema.field(idx) if idx >= 0 else None
            except (KeyError, ValueError):
                f = None
            if f is None:
                out.append(c)
                continue
            typ = c.type
            if _is_timestamp_type(f.type):
                unit = f.type.unit
                suffix = {"s": "_S", "ms": "_MS", "us": "", "ns": "_NS"}[unit]
                tz = f.type.tz
                typ = "TIMESTAMP WITH TIME ZONE" if tz else f"TIMESTAMP{suffix}"
            out.append(ColumnInfo(c.name, typ, nullable=f.nullable))
        return out


class CsvSource(RelationSource):
    def __init__(
        self,
        session: Session,
        alias: str,
        path: Path,
        *,
        all_varchar: bool = False,
    ) -> None:
        if not path.is_file():
            msg = f"csv file not found: {path}"
            raise SourceError(msg)
        opts = ", all_varchar=true" if all_varchar else ""
        view_sql = (
            f"SELECT * FROM read_csv({quote_literal(str(path))}, header=true, sample_size=-1{opts})"
        )
        try:
            session.create_view(alias, view_sql)
        except Exception as exc:
            msg = f"could not read csv file {path}: {exc}"
            raise SourceError(msg) from exc
        super().__init__(session, alias, quote_ident(alias))
        self.path = path


def bind_file(
    session: Session,
    alias: str,
    path: Path,
    *,
    all_varchar: bool = False,
) -> BoundSource:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return ParquetSource(session, alias, path)
    if suffix in {".csv", ".tsv", ".txt"}:
        return CsvSource(session, alias, path, all_varchar=all_varchar)
    msg = f"unsupported file extension {suffix!r} for {path}; expected .parquet or .csv"
    raise SourceError(msg)


__all__ = ["ColumnInfo", "CsvSource", "ParquetSource", "bind_file", "quote_ident"]
