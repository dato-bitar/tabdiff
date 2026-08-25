"""M1 acceptance: every local source delivers Arrow schema + row count."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb as duckdb_mod
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tabdiff.errors import SourceError
from tabdiff.session import Session
from tabdiff.source import SourceOptions, bind_source, parse_source_spec


@pytest.fixture()
def session() -> Any:
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def people_parquet(tmp_path: Path) -> Path:
    t = pa.table(
        {
            "id": pa.array([1, 2, 3], type=pa.int64()),
            "name": pa.array(["alice", "bob", "carol"]),
            "score": pa.array([1.5, 2.5, None], type=pa.float64()),
        }
    )
    p = tmp_path / "people.parquet"
    pq.write_table(t, p)
    return p


@pytest.fixture()
def people_csv(tmp_path: Path) -> Path:
    p = tmp_path / "people.csv"
    p.write_text("id,name,score\n1,alice,1.5\n2,bob,2.5\n3,carol,\n", encoding="utf-8")
    return p


@pytest.fixture()
def people_duckdb(tmp_path: Path) -> Path:
    p = tmp_path / "people.duckdb"
    con = duckdb_mod.connect(str(p))
    con.execute("CREATE TABLE people (id BIGINT, name VARCHAR, active BOOLEAN)")
    con.execute("INSERT INTO people VALUES (1,'a',true),(2,'b',false)")
    con.close()
    return p


class TestParquetSource:
    def test_columns_and_count(self, session: Any, people_parquet: Path) -> None:
        src = bind_source(session, "l", str(people_parquet))
        cols = {c.name: c.type for c in src.columns()}
        assert cols == {"id": "BIGINT", "name": "VARCHAR", "score": "DOUBLE"}
        assert src.count() == 3

    def test_arrow_schema(self, session: Any, people_parquet: Path) -> None:
        src = bind_source(session, "l", str(people_parquet))
        schema = src.arrow_schema()
        assert schema.field("id").type == pa.int64()

    def test_missing_file_raises(self, session: Any, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="not found"):
            bind_source(session, "l", str(tmp_path / "nope.parquet"))


class TestCsvSource:
    def test_columns_and_count(self, session: Any, people_csv: Path) -> None:
        src = bind_source(session, "r", str(people_csv))
        assert src.count() == 3
        cols = {c.name: c.type for c in src.columns()}
        # sniffer sees an empty cell in score; duckdb may type it DOUBLE
        assert set(cols) == {"id", "name", "score"}
        assert cols["id"] in {"BIGINT", "INTEGER"}
        assert cols["name"] == "VARCHAR"

    def test_all_varchar(self, session: Any, people_csv: Path) -> None:
        src = bind_source(session, "r", str(people_csv), options=SourceOptions(all_varchar=True))
        cols = {c.name: c.type for c in src.columns()}
        assert all(t == "VARCHAR" for t in cols.values())

    def test_bad_extension(self, session: Any, tmp_path: Path) -> None:
        f = tmp_path / "data.xlsx"
        f.write_text("nope")
        with pytest.raises(SourceError, match="cannot interpret source"):
            bind_source(session, "l", str(f))


class TestDuckDBFileSource:
    def test_bind_and_count(self, session: Any, people_duckdb: Path) -> None:
        src = bind_source(session, "d", f"duckdb://{people_duckdb.as_posix()}/people")
        assert src.count() == 2
        cols = {c.name: c.type for c in src.columns()}
        assert cols == {"id": "BIGINT", "name": "VARCHAR", "active": "BOOLEAN"}

    def test_missing_table(self, session: Any, people_duckdb: Path) -> None:
        with pytest.raises(SourceError, match="not found in duckdb"):
            bind_source(session, "d", f"duckdb://{people_duckdb.as_posix()}/missing")

    def test_invalid_spec(self, session: Any, people_duckdb: Path) -> None:
        with pytest.raises(SourceError, match="invalid duckdb spec"):
            bind_source(session, "d", f"duckdb://{people_duckdb.as_posix()}")


class TestSpecParsing:
    def test_postgres_url_with_schema(self) -> None:
        kind, path, url, table = parse_source_spec("postgres://u:p@h:5432/mydb/public/events")
        assert kind == "postgres"
        assert path is None
        assert url == "postgres://u:p@h:5432/mydb/public/events"
        assert table == "public.events"

    def test_postgres_url_without_schema(self) -> None:
        kind, _path, _url, table = parse_source_spec("postgresql://u@h/db/events")
        assert kind == "postgres"
        assert table == "events"

    def test_unknown_spec(self) -> None:
        with pytest.raises(SourceError, match="cannot interpret source"):
            parse_source_spec("mysql://x/y")

    def test_relative_paths(self) -> None:
        kind, path, _, _ = parse_source_spec("sub/dir/table.parquet")
        assert kind == "parquet"
        assert path is not None and path.as_posix() == "sub/dir/table.parquet"
