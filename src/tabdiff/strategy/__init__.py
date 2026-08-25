"""Diff strategies."""

from tabdiff.strategy.hash_diff import run_hash_diff
from tabdiff.strategy.join_diff import run_join_diff

__all__ = ["run_hash_diff", "run_join_diff"]
