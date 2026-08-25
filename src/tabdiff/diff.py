"""High-level orchestration: bind sources, pick strategy, run, attach stats."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit

from tabdiff.canon import CompareOptions
from tabdiff.keycheck import guess_key
from tabdiff.model import DiffReport
from tabdiff.normalize import Canon
from tabdiff.schema_diff import parse_type as schema_parse_type
from tabdiff.session import Session
from tabdiff.source import SourceOptions, bind_source
from tabdiff.source.base import BoundSource
from tabdiff.source.postgres import PostgresSource
from tabdiff.stats import compute_stats_drift
from tabdiff.strategy.hash_diff import run_hash_diff
from tabdiff.strategy.join_diff import run_join_diff
from tabdiff.strategy.keyless import run_keyless_diff


@dataclass(frozen=True)
class RunOptions:
    key: tuple[str, ...] = ()
    key_less: bool = False
    strategy: str = "auto"  # auto | join | hash | keyless
    opts: CompareOptions = field(default_factory=CompareOptions)
    examples_n: int | None = 20
    full: bool = False
    include_schema: bool = True
    include_counts: bool = True
    include_values: bool = True
    include_stats: bool = True
    all_varchar: bool = False
    leaf_rows: int = 8192


def _same_postgres_instance(a: str, b: str) -> bool:
    pa_, pb = urlsplit(a), urlsplit(b)
    return (pa_.hostname, pa_.port) == (pb.hostname, pb.port)


def choose_strategy(left_src: BoundSource, right_src: BoundSource, options: RunOptions) -> str:
    if options.strategy != "auto":
        return options.strategy
    if options.key_less:
        return "keyless"
    l_pg, r_pg = isinstance(left_src, PostgresSource), isinstance(right_src, PostgresSource)
    if (
        l_pg
        and r_pg
        and not _same_postgres_instance(
            cast(PostgresSource, left_src).url, cast(PostgresSource, right_src).url
        )
    ):
        return "hash"
    if l_pg != r_pg:
        return "hash"
    return "join"


def resolve_key(
    session: Session,
    left_src: BoundSource,
    right_src: BoundSource,
    options: RunOptions,
) -> list[str]:
    _ = session
    if options.key:
        return [k.strip() for k in options.key]
    return guess_key(
        [c.name for c in left_src.columns()],
        [c.name for c in right_src.columns()],
        l_pk_hint=left_src.primary_key_hint(),
        r_pk_hint=right_src.primary_key_hint(),
    )


def run_diff(
    left_spec: str,
    right_spec: str,
    options: RunOptions,
    session: Session | None = None,
) -> DiffReport:
    """Full diff of two source specs. Raises TabDiffError on fatal problems."""
    started = time.monotonic()
    owns_session = session is None
    s = session or Session()
    try:
        file_opts = SourceOptions(all_varchar=options.all_varchar)
        left_src = bind_source(s, "l", left_spec, file_opts)
        right_src = bind_source(s, "r", right_spec, file_opts)

        strategy = choose_strategy(left_src, right_src, options)

        if strategy == "keyless":
            report = run_keyless_diff(
                s,
                left_src,
                right_src,
                opts=options.opts,
                include_schema=options.include_schema,
                include_counts=options.include_counts,
            )
        else:
            key = resolve_key(s, left_src, right_src, options)
            report = _run_keyed(s, left_src, right_src, key, options, strategy)

        if options.include_stats and strategy != "keyless":
            r_names = {c.name for c in right_src.columns()}
            types_map: dict[str, Canon | None] = {
                c.name: schema_parse_type(c).canon for c in left_src.columns() if c.name in r_names
            }
            report.stats = compute_stats_drift(s, left_src, right_src, types_map)

        report.meta.duration_s = time.monotonic() - started
        return report
    finally:
        if owns_session:
            s.close()


def _run_keyed(
    session: Session,
    left_src: BoundSource,
    right_src: BoundSource,
    key: list[str],
    options: RunOptions,
    strategy: str,
) -> DiffReport:
    if strategy == "hash":
        return run_hash_diff(
            session,
            left_src,
            right_src,
            key_cols=key,
            opts=options.opts,
            examples_n=options.examples_n,
            full=options.full,
            include_schema=options.include_schema,
            include_counts=options.include_counts,
            include_values=options.include_values,
            leaf_rows=options.leaf_rows,
        )
    return run_join_diff(
        session,
        left_src,
        right_src,
        key_cols=key,
        opts=options.opts,
        examples_n=options.examples_n,
        full=options.full,
        include_schema=options.include_schema,
        include_counts=options.include_counts,
        include_values=options.include_values,
    )


__all__ = ["RunOptions", "choose_strategy", "resolve_key", "run_diff"]
