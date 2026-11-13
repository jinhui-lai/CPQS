from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple


@dataclass(frozen=True)
class CardinalityCertificate:
    """Schema-side access certificate used by generation and ablation.

    Distinct from :class:`CardinalityOracle`, which is the per-query bound
    attached to a synthesized ``QuerySpec``. A certificate records how a
    schema feature justifies an access bound before any query is written.
    """

    mechanism: str  # unique lookup | ordered | partition prune | contradiction | join elimination | rewrite
    unit: str  # row | index entry | partition row | mv row
    bound: int
    table: str
    index: Optional[str] = None
    precondition: str = ""
    dialects: Tuple[str, ...] = ()
    priority: str = "P0"  # P0 | P1 | P2


@dataclass(frozen=True)
class CardinalityOracle:
    """Sound expected access cardinality for one constructed query.

    If ``lower_bound == upper_bound`` the oracle is exact. ``candidate_rows``
    stores primary-key identifiers when they are known from generated data.
    """

    lower_bound: int
    upper_bound: int
    exact: bool
    candidate_rows: Optional[FrozenSet[int]] = None

    def __post_init__(self) -> None:
        if self.lower_bound < 0 or self.upper_bound < 0:
            raise ValueError("cardinality bounds must be non-negative")
        if self.lower_bound > self.upper_bound:
            raise ValueError("lower_bound must be <= upper_bound")
        object.__setattr__(self, "exact", self.lower_bound == self.upper_bound)

    @staticmethod
    def exact_count(n: int, rows: Optional[FrozenSet[int]] = None) -> "CardinalityOracle":
        return CardinalityOracle(n, n, True, rows)

    @staticmethod
    def interval(lower: int, upper: int, rows: Optional[FrozenSet[int]] = None) -> "CardinalityOracle":
        return CardinalityOracle(lower, upper, lower == upper, rows)

    def tightness(self, n_rows: int) -> float:
        if n_rows <= 0:
            return 1.0
        return self.upper_bound / n_rows

    def is_tight(self, n_rows: int, max_ratio: float) -> bool:
        return self.tightness(n_rows) <= max_ratio

    def describe(self) -> str:
        if self.exact:
            return f"[{self.lower_bound},{self.upper_bound}] exact"
        return f"[{self.lower_bound},{self.upper_bound}] interval"


def and_oracles(left: CardinalityOracle, right: CardinalityOracle) -> CardinalityOracle:
    """Conjunctive combination of two efficient access paths."""
    if left.candidate_rows is not None and right.candidate_rows is not None:
        rows = left.candidate_rows & right.candidate_rows
        return CardinalityOracle.exact_count(len(rows), rows)
    upper = min(left.upper_bound, right.upper_bound)
    return CardinalityOracle.interval(0, upper)


def and_residual(efficient: CardinalityOracle, residual_true_rows: Optional[FrozenSet[int]]) -> CardinalityOracle:
    """Efficient path AND an ordinary predicate.

    If the residual can be evaluated on known candidates, the oracle stays exact.
    Otherwise it weakens to ``[0, U]``.
    """
    if efficient.candidate_rows is not None and residual_true_rows is not None:
        rows = efficient.candidate_rows & residual_true_rows
        return CardinalityOracle.exact_count(len(rows), rows)
    return CardinalityOracle.interval(0, efficient.upper_bound)


def or_oracles(left: CardinalityOracle, right: CardinalityOracle, n_rows: int) -> Optional[CardinalityOracle]:
    """Disjunctive combination. Returns None when the bound is uncontrolled."""
    if left.candidate_rows is not None and right.candidate_rows is not None:
        rows = left.candidate_rows | right.candidate_rows
        return CardinalityOracle.exact_count(len(rows), rows)
    lower = max(left.lower_bound, right.lower_bound)
    upper = min(n_rows, left.upper_bound + right.upper_bound)
    if upper >= n_rows:
        return None
    return CardinalityOracle.interval(lower, upper)
