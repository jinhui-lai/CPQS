from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from cpqs.core.oracle import CardinalityOracle


@dataclass(frozen=True)
class AccessOccurrence:
    """One base-relation occurrence and its expected access bound.

    The pivot is the occurrence the bandit scores. Other occurrences are
    still validated on their own record-examination counters.
    """

    table: str
    upper_bound: int
    column: Optional[str] = None
    pivot: bool = False


@dataclass
class QuerySpec:
    """Dialect-agnostic SELECT used by access-path generators."""

    path_kind: str
    select_list: str
    from_clause: str
    oracle: CardinalityOracle
    where: Optional[str] = None
    having: Optional[str] = None
    group_by: Optional[str] = None
    order_by: Optional[str] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    comment: str = ""
    notes: List[str] = field(default_factory=list)
    skip_tightness: bool = False
    # Cardinality pivot: the base-relation occurrence whose bound is tracked.
    pivot_table: Optional[str] = None
    pivot_column: Optional[str] = None
    # "" for a seed; otherwise the query-arm name that produced this spec.
    mutation: str = ""
    # seed | preservation | tightening | composition
    bound_outcome: str = ""
    # Optional leading clause (WITH ...). Kept outside the SELECT so mutations
    # can wrap a pivot without rewriting the oracle.
    prefix: str = ""
    # Per-input bounds. Empty until the seed or a join mutation fills them.
    occurrences: Tuple[AccessOccurrence, ...] = ()

    def sql(self, dialect: str) -> str:
        parts = [f"SELECT {self.select_list}", f"FROM {self.from_clause}"]
        if self.where:
            parts.append(f"WHERE {self.where}")
        if self.group_by:
            parts.append(f"GROUP BY {self.group_by}")
        if self.having:
            parts.append(f"HAVING {self.having}")
        if self.order_by:
            parts.append(f"ORDER BY {self.order_by}")
        limit_sql = render_limit(dialect, self.limit, self.offset)
        if limit_sql:
            parts.append(limit_sql)
        body = " ".join(parts)
        if self.prefix:
            return f"{self.prefix} {body}"
        return body


def render_limit(dialect: str, limit: Optional[int], offset: Optional[int]) -> str:
    if limit is None and (offset is None or offset == 0):
        return ""
    lim = 0 if limit is None else limit
    off = 0 if offset is None else offset
    if dialect == "oracle":
        if off:
            return f"OFFSET {off} ROWS FETCH NEXT {lim} ROWS ONLY"
        return f"FETCH FIRST {lim} ROWS ONLY"
    if off:
        return f"LIMIT {lim} OFFSET {off}"
    return f"LIMIT {lim}"
