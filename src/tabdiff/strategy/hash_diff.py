"""hashdiff: hierarchical checksum bisecting for sources in different engines.

Both sides compute per-row hashes and per-segment signatures IN THEIR OWN
ENGINE from identical canonical semantics (see tabdiff.canon). Segments whose
signatures match are discarded; mismatching segments recurse into finer
sub-segments until small enough to pull, then they are compared locally with
the exact joindiff machinery - one code path decides final verdicts.

Portability rules (verified by tests):
- md5() agrees byte-for-byte between DuckDB and Postgres.
- hex-digit -> integer conversion uses strpos() arithmetic only.
- aggregate components stay below 2^63 (prime p < 2^31 products), so sums
  cannot overflow in either engine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from tabdiff.canon import (
    CompareOptions,
    Engine,
    canonical_field_sql,
    effective_precision,
    escaped_field_sql,
)
from tabdiff.keycheck import check_key_usable
from tabdiff.model import (
    CellExample,
    ColumnValueDiff,
    CountsDiff,
    DiffMeta,
    DiffReport,
    ValueDiffResult,
)
from tabdiff.normalize import Canon, TypeInfo
from tabdiff.schema_diff import diff_schemas, parse_type
from tabdiff.source.base import ColumnInfo
from tabdiff.strategy.join_diff import FULL_EXAMPLE_CAP, run_join_diff

if TYPE_CHECKING:
    from tabdiff.session import Session
    from tabdiff.source.base import BoundSource

PRIME = 2147483629
HEX_CHARS = "0123456789abcdef"
INITIAL_WIDTH = 16
WIDTH_STEP = 4
MAX_WIDTH = 32
DEFAULT_LEAF_ROWS = 8192


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def hex_prefix_to_int_sql(hex_expr: str, n_digits: int, offset: int = 0) -> str:
    """Portable arithmetic turning n hex digits into a BIGINT."""
    if n_digits > 15:
        raise ValueError("at most 15 hex digits fit into a signed bigint")
    out = ""
    for i in range(offset, offset + n_digits):
        d = f"(strpos('{HEX_CHARS}', substr({hex_expr}, {i + 1}, 1)) - 1)"
        # ::BIGINT keeps postgres from overflowing int4 (duckdb widens
        # implicitly, postgres does not)
        out = f"{d}::BIGINT" if not out else f"((({out})::BIGINT) * 16 + ({d})::BIGINT)"
    return out


@dataclass(frozen=True)
class _SideSpec:
    src: Any  # BoundSource-like (duck-typed; duckdb itself is untyped)
    ti: dict[str, TypeInfo]
    key_cols: list[str]
    compare_names: list[str]
    precisions: dict[str, int | None]
    opts: CompareOptions

    def _field_canon(self, name: str, engine: Engine) -> str:
        interpret = self.ti[name].canon is Canon.TIMESTAMP_NAIVE and bool(self.opts.assume_tz)
        return canonical_field_sql(
            engine,
            _qi(name),
            self.ti[name],
            self.opts,
            target_precision=self.precisions.get(name),
            interpret_naive_tz=interpret,
        )


def _build_hk(spec: _SideSpec, engine: Engine) -> str:
    parts = []
    for k in spec.key_cols:
        canon = canonical_field_sql(
            engine,
            _qi(k),
            spec.ti[k],
            spec.opts,
            target_precision=spec.precisions.get("_key_" + k),
        )
        parts.append(escaped_field_sql(canon))
    return f"md5({'||'.join(parts)})"


def _pipeline_sql(spec: _SideSpec, engine: Engine) -> str:
    """Inner query: hk (key hash), h1/h2/pr (row-hash signature parts)."""
    rh_parts = [escaped_field_sql(spec._field_canon(k, engine)) for k in spec.key_cols]
    rh_parts += [escaped_field_sql(spec._field_canon(n, engine)) for n in spec.compare_names]
    rh = f"md5({'||'.join(rh_parts)})"
    hk = _build_hk(spec, engine)
    h1 = hex_prefix_to_int_sql(rh, 15, 0)
    h2 = hex_prefix_to_int_sql(rh, 15, 15)
    pr = f"((({h1} % {PRIME}) * ({h2} % {PRIME})) % {PRIME})"
    return f"SELECT {hk} AS hk, {h1} AS h1, {h2} AS h2, {pr} AS pr FROM {{rel}}"


@dataclass
class _ScanResult:
    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    sigs: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    def grouped(self, old_width: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for prefix, n in self.counts.items():
            out[prefix[:old_width]] = out.get(prefix[:old_width], 0) + n
        return out


def _exec_rows(session: Session, spec: _SideSpec, engine: str, sql: str) -> list[tuple[Any, ...]]:
    if engine == "postgres" and hasattr(spec.src, "execute_remote"):
        tbl = spec.src.execute_remote(sql)
        cols = [tbl.column(i).to_pylist() for i in range(tbl.num_columns)]
        return [tuple(row) for row in zip(*cols, strict=True)] if cols else []
    return session.rows(sql)


def _rel_for(spec: _SideSpec, engine: str) -> str:
    """Relation fragment matching the executing engine.

    Pushed-down postgres SQL references the table by its POSTGRES names
    (schema.table); duckdb-side execution goes through the attach alias.
    """
    if engine == "postgres":
        return str(spec.src.remote_relation_sql())
    return str(spec.src.relation_sql())


def _scan(
    session: Session,
    spec: _SideSpec,
    engine: Engine,
    pipe_tpl: str,
    width: int,
    restrict: tuple[int, list[str]] | None = None,
) -> _ScanResult:
    sql_inner = pipe_tpl.format(rel=_rel_for(spec, engine))
    if restrict:
        w, prefixes = restrict
        plist = ", ".join("'" + p + "'" for p in prefixes)
        sql_inner = f"SELECT * FROM ({sql_inner}) q WHERE substr(q.hk, 1, {w}) IN ({plist})"
    sql = (
        f"SELECT substr(hk, 1, {width}) AS b, count(*) AS n, "
        f"sum(h1) AS s1, sum(h2) AS s2, sum(pr) AS s3 FROM ({sql_inner}) q GROUP BY 1"
    )
    res = _ScanResult()
    for b, n, s1, s2, sp in _exec_rows(session, spec, engine, sql):
        prefix = str(b)
        res.counts[prefix] = int(n)
        res.sigs[prefix] = (int(s1), int(s2), int(sp))
        res.total += int(n)
    return res


def _differing(left: _ScanResult, right: _ScanResult) -> list[str]:
    bad = [
        p
        for p in set(left.sigs) | set(right.sigs)
        if left.sigs.get(p) != right.sigs.get(p) or left.counts.get(p) != right.counts.get(p)
    ]
    return sorted(bad)


def run_hash_diff(
    session: Session,
    left_src: BoundSource,
    right_src: BoundSource,
    *,
    key_cols: list[str],
    opts: CompareOptions,
    leaf_rows: int = DEFAULT_LEAF_ROWS,
    examples_n: int | None = 20,
    full: bool = False,
    include_schema: bool = True,
    include_counts: bool = True,
    include_values: bool = True,
) -> DiffReport:
    started = time.monotonic()
    l_cols = left_src.columns()
    r_cols = right_src.columns()

    schema_diff, pairs = diff_schemas(
        l_cols, r_cols, assume_tz=opts.assume_tz, ts_precision=opts.ts_precision
    )
    assumptions = list(schema_diff.assumptions)
    warnings = list(schema_diff.warnings)

    l_names = {c.name for c in l_cols}
    r_names = {c.name for c in r_cols}
    for k in key_cols:
        missing = "left" if k not in l_names else ("right" if k not in r_names else None)
        if missing:
            msg = f"key column {k!r} not present on the {missing} side"
            raise KeyError(msg)

    check_key_usable(session, left_src, key_cols, side="left")
    check_key_usable(session, right_src, key_cols, side="right")

    compare_names = [c.name for c in l_cols if c.name in pairs]
    l_ti = {c.name: parse_type(c) for c in l_cols}
    r_ti = {c.name: parse_type(c) for c in r_cols}

    precisions: dict[str, int | None] = {
        name: effective_precision(lt, rt, opts) for name, (lt, rt, _pc) in pairs.items()
    }
    for k in key_cols:
        lt, rt = l_ti[k], r_ti[k]
        if lt.is_temporal or rt.is_temporal:
            precisions[f"_key_{k}"] = effective_precision(lt, rt, opts)

    engines: dict[str, Engine] = {
        "left": cast(Engine, left_src.preferred_engine()),
        "right": cast(Engine, right_src.preferred_engine()),
    }
    execution_path = {
        side: ("pushdown" if eng == "postgres" else "local-scan") for side, eng in engines.items()
    }
    warnings.append(
        f"hashdiff execution: left={engines['left']}, right={engines['right']} "
        "(postgres means checksums are computed inside postgres)"
    )
    # drop the informational line when both sides are plain duckdb
    if engines == {"left": "duckdb", "right": "duckdb"}:
        warnings.pop()

    l_spec = _SideSpec(
        src=left_src,
        ti=l_ti,
        key_cols=key_cols,
        compare_names=compare_names,
        precisions=precisions,
        opts=opts,
    )
    r_spec = _SideSpec(
        src=right_src,
        ti=r_ti,
        key_cols=key_cols,
        compare_names=compare_names,
        precisions=precisions,
        opts=opts,
    )
    l_engine, r_engine = engines["left"], engines["right"]

    l_pipe = _pipeline_sql(l_spec, l_engine)
    r_pipe = _pipeline_sql(r_spec, r_engine)

    # ---- bisect -----------------------------------------------------------------
    width = INITIAL_WIDTH
    l_res = _scan(session, l_spec, l_engine, l_pipe, width)
    r_res = _scan(session, r_spec, r_engine, r_pipe, width)
    left_total, right_total = l_res.total, r_res.total
    bad = _differing(l_res, r_res)

    pulls: list[tuple[int, list[str]]] = []
    while bad and width < MAX_WIDTH:
        counts_l = l_res.grouped(width)
        counts_r = r_res.grouped(width)
        leaves_now: list[str] = []
        refine: list[str] = []
        for p in bad:
            n = max(counts_l.get(p, 0), counts_r.get(p, 0))
            if n <= leaf_rows:
                leaves_now.append(p)
            else:
                refine.append(p)
        if leaves_now:
            pulls.append((width, leaves_now))
        if not refine:
            bad = []  # everything differing was queued for pulling already
            break
        new_width = min(width + WIDTH_STEP, MAX_WIDTH)
        l_res = _scan(session, l_spec, l_engine, l_pipe, new_width, (width, refine))
        r_res = _scan(session, r_spec, r_engine, r_pipe, new_width, (width, refine))
        width = new_width
        bad = _differing(l_res, r_res)
    if bad:  # width hit MAX_WIDTH: pull everything that still differs
        pulls.append((MAX_WIDTH, bad))

    # ---- pull differing leaves, compare with joindiff ----------------------------
    left_only = right_only = changed = both_seen = 0
    value_columns: dict[str, int] = {}
    example_cells: dict[str, list[CellExample]] = {}
    room_per_col = FULL_EXAMPLE_CAP if full else (examples_n or 0)

    for w, prefixes in pulls:
        l_arrow = _fetch_arrow(session, l_spec, l_engine, l_pipe, w, prefixes, l_cols)
        r_arrow = _fetch_arrow(session, r_spec, r_engine, r_pipe, w, prefixes, r_cols)
        if l_arrow.num_rows == 0 and r_arrow.num_rows == 0:
            continue
        session.con.register("hd_left", l_arrow)
        session.con.register("hd_right", r_arrow)
        lv = _ArrowView(session, "hd_left", list(l_arrow.schema.names))
        rv = _ArrowView(session, "hd_right", list(r_arrow.schema.names))
        try:
            slice_report = run_join_diff(
                session,
                lv,  # type: ignore[arg-type]
                rv,  # type: ignore[arg-type]
                key_cols=key_cols,
                opts=opts,
                examples_n=None if full else examples_n,
                full=full,
                include_schema=False,
                # counts are needed per-slice to accumulate only/both;
                # slice *totals* are meaningless and never used.
                include_counts=True,
                include_values=True,
            )
        finally:
            session.con.unregister("hd_left")
            session.con.unregister("hd_right")
        slice_counts = slice_report.counts
        if slice_counts is not None:
            left_only += slice_counts.left_only
            right_only += slice_counts.right_only
            both_seen += slice_counts.both
        assert slice_report.values is not None  # include_values=True above
        changed += slice_report.values.changed_rows
        for cd in slice_report.values.columns:
            value_columns[cd.column] = value_columns.get(cd.column, 0) + cd.mismatched_rows
            bucket = example_cells.setdefault(cd.column, [])
            for ex in cd.examples:
                if len(bucket) < max(room_per_col, len(bucket)):
                    bucket.append(ex)

    values = ValueDiffResult(changed_rows=changed)
    if include_values:
        for colname in sorted(value_columns):
            values.columns.append(
                ColumnValueDiff(
                    column=colname,
                    mismatched_rows=value_columns[colname],
                    examples=example_cells.get(colname, []),
                )
            )

    report = DiffReport(
        meta=DiffMeta(
            strategy="hash",
            key=list(key_cols),
            assumptions=assumptions,
            warnings=warnings,
            execution_path=execution_path,
        ),
        schema=schema_diff if include_schema else None,
        counts=CountsDiff(
            left_total=left_total,
            right_total=right_total,
            left_only=left_only,
            right_only=right_only,
            both=left_total - left_only,
        )
        if include_counts
        else None,
        values=values,
    )
    report.meta.duration_s = time.monotonic() - started
    _ = both_seen
    return report


class _ArrowView:
    """Minimal BoundSource-like wrapper around a registered Arrow table."""

    def __init__(self, session: Session, alias: str, names: list[str]) -> None:
        self.session = session
        self.alias = alias
        self.names = names

    @property
    def engine(self) -> str:
        return "duckdb"

    def relation_sql(self, *, columns: list[str] | None = None) -> str:
        proj = "*" if columns is None else ", ".join(_qi(c) for c in columns)
        return f"(SELECT {proj} FROM {_qi(self.alias)})"

    def columns(self) -> list[ColumnInfo]:
        rows = self.session.rows(f"DESCRIBE {self.relation_sql()}")
        return [ColumnInfo(str(r[0]), str(r[1]), str(r[2]).upper() != "NO") for r in rows]

    def count(self) -> int:
        return int(self.session.scalar(f"SELECT count(*) FROM {self.relation_sql()}"))


def _fetch_arrow(
    session: Session,
    spec: _SideSpec,
    engine: Engine,
    pipe_tpl: str,
    width: int,
    prefixes: list[str],
    cols: list[ColumnInfo],
) -> Any:
    plist = ", ".join("'" + p + "'" for p in prefixes)
    hk = _build_hk(spec, engine)
    names = [c.name for c in cols]
    proj = ", ".join(_qi(n) for n in names)
    inner = f"SELECT {proj}, {hk} AS __hk FROM {{rel}}"
    sql_inner = inner.format(rel=_rel_for(spec, engine))
    sql = f"SELECT {proj} FROM ({sql_inner}) q WHERE substr(q.__hk, 1, {width}) IN ({plist})"
    if engine == "postgres" and hasattr(spec.src, "execute_remote"):
        return spec.src.execute_remote(sql)
    return session.arrow(sql)
