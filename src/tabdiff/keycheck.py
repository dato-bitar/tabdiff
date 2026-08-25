"""Key guessing and uniqueness enforcement."""

from __future__ import annotations

from dataclasses import dataclass

from tabdiff.errors import KeyNotUnique, NoKeyFound
from tabdiff.session import Session, quote_ident
from tabdiff.source.base import BoundSource

KEY_CANDIDATES = ["id", "pk", "uuid", "guid"]


@dataclass(frozen=True)
class KeyCheckResult:
    samples: list[tuple[object, ...]]  # up to 3 duplicate key groups


def guess_key(
    l_names: list[str],
    r_names: list[str],
    *,
    l_pk_hint: list[str] | None = None,
    r_pk_hint: list[str] | None = None,
) -> list[str]:
    """Pick a key without user input - loudly, never silently.

    Precedence: primary keys reported by both sides > well-known names >
    error with a helpful message.
    """
    shared = [c for c in l_names if c in r_names]

    if (
        l_pk_hint
        and r_pk_hint
        and sorted(l_pk_hint) == sorted(r_pk_hint)
        and all(h in shared for h in l_pk_hint)
    ):
        return list(l_pk_hint)

    for cand in KEY_CANDIDATES:
        if cand in shared:
            return [cand]

    msg = (
        "no key given and none could be guessed. Shared columns: "
        f"{shared[:12]}{'...' if len(shared) > 12 else ''}. "
        "Pass --key col1,col2 explicitly."
    )
    raise NoKeyFound(msg)


def check_key_usable(session: Session, src: BoundSource, key_cols: list[str], *, side: str) -> None:
    """Abort when the key is not unique - a dup-keyed diff is worse than none."""
    rel = src.relation_sql()
    keys = ", ".join(quote_ident(k) for k in key_cols)

    dupes_sql = (
        f"SELECT {keys}, count(*) AS n "
        f"FROM {rel} GROUP BY {keys} HAVING count(*) > 1 ORDER BY n DESC LIMIT 3"
    )
    samples = session.rows(dupes_sql)
    if samples:
        pretty = "; ".join(
            "(" + ", ".join(repr(v) for v in row[:-1]) + f") x{row[-1]}" for row in samples
        )
        msg = (
            f"key {key_cols} is not unique on the {side} side; "
            f"example duplicate groups: {pretty}. "
            "tabdiff refuses to produce meaningless results; fix the key."
        )
        raise KeyNotUnique(msg)
