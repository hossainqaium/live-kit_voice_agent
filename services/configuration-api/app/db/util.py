"""Small helpers for reading query results.

``dict(result.all())`` is the obvious way to turn a two-column SELECT into a
lookup, but SQLAlchemy returns ``Row`` objects rather than tuples, so the
annotation does not hold and every call site ends up with a type ignore. One
typed helper is better than that comment repeated a dozen times.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

K = TypeVar("K")
V = TypeVar("V")


def as_lookup(rows: Sequence[Any]) -> dict[Any, Any]:
    """Turn the rows of a two-column SELECT into a dict keyed by the first."""
    return {row[0]: row[1] for row in rows}
