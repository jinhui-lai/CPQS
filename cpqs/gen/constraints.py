"""Constraint-based zero-access cardinality oracles (P0).

1. ``null_contradiction`` — column is ``NOT NULL``, so ``col IS NULL`` examines
   zero rows. Applies on every dialect that reports access rows for an empty
   result (const/Impossible WHERE / Dual / Result).

2. ``outer_join_elim`` — ``SELECT left_only FROM left LEFT JOIN right ON
   left.k = right.k WHERE left.k IN (...)``. Right key is unique and unused in
   the SELECT list, so a justified plan never accesses ``right``. Bound is the
   left filter size ``m``; a full build/scan of ``right`` (``N``) violates it.
"""

from __future__ import annotations

import random
from typing import Optional

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData, in_list
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import QuerySpec
from cpqs.gen.predicates import q


def _pick_table(catalog: Catalog, rng: random.Random) -> TableData:
    return rng.choice(catalog.ordered_tables())


def gen_null_contradiction(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """``NOT NULL`` column with ``IS NULL`` ⇒ expected access 0."""
    del cfg
    table = _pick_table(catalog, rng)
    cols = table.not_null_columns()
    if not cols:
        return None
    # Prefer unique leading columns so a missed fold cannot hide behind a
    # selective index probe that still looks "small".
    preferred = [c for c in cols if c in table.unique_leading_columns()]
    col = rng.choice(preferred or cols)
    return QuerySpec(
        path_kind="null_contradiction",
        select_list="*",
        from_clause=table.name,
        where=f"{col} IS NULL",
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"{col} IS NOT NULL ⇒ {col} IS NULL is empty",
        notes=["schema NOT NULL proves zero access; no index or row count needed"],
        skip_tightness=True,
    )


def gen_outer_join_elim(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """LEFT JOIN on unique right key, SELECT left columns only ⇒ right access 0.

    Bound is the left unique IN size. If the optimizer keeps the join and hash-
    builds / scans the full right table, max access rows jump to ``N``.
    """
    del cfg
    tables = catalog.ordered_tables()
    if len(tables) < 2:
        return None
    left, right = rng.sample(tables, 2)
    lcols = left.unique_leading_columns()
    rcols = right.unique_leading_columns()
    if not lcols or not rcols:
        return None
    lcol = rng.choice(lcols)
    rcol = rng.choice(rcols)
    shared = sorted(set(left.pk_set()) & set(right.pk_set()))
    if len(shared) < 4:
        return None
    m = rng.randint(2, min(8, len(shared)))
    keys = rng.sample(shared, m)
    # Only left-side columns in the projection — right is unused.
    left_proj = [c.name for c in left.columns if c.name != rcol][:3] or [lcol]
    select_list = ", ".join(f"{q(left.name, c)}" for c in left_proj)
    from_clause = (
        f"{left.name} LEFT JOIN {right.name} "
        f"ON {q(left.name, lcol)} = {q(right.name, rcol)}"
    )
    where = f"{q(left.name, lcol)} IN {in_list(keys)}"
    return QuerySpec(
        path_kind="outer_join_elim",
        select_list=select_list,
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"LEFT JOIN on unique {right.name}.{rcol} unused in SELECT; right access 0",
        notes=[
            "unique right key + unused projection ⇒ table elimination",
            "bound is |left filter|; a full right scan/hash build is a miss",
        ],
    )
