"""Schema-driven access certificates beyond the core unique/ordered/partition set.

P0
  expr_abs_lookup / expr_abs_range — INDEX on ABS(k) (or generated k_abs)
  partial_pk_lookup — INDEX(pk) WHERE k <= T, query implies the predicate

P1
  index_merge_or — INDEX(a), INDEX(b) with OR; U ≤ m1+m2
  covering_lookup — INDEX(k) INCLUDE (payload); SELECT k, payload WHERE k = v
  inner_join_elim_fk — FK + NOT NULL; SELECT left-only INNER JOIN eliminates right
"""

from __future__ import annotations

import random
import re
from typing import List, Optional, Tuple

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData, in_list, sql_literal
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import QuerySpec
from cpqs.gen.predicates import q
from cpqs.gen.schema import INDEX_MERGE_DBS, PARTIAL_INDEX_DBS


def _pick_table(catalog: Catalog, rng: random.Random) -> TableData:
    return rng.choice(catalog.ordered_tables())


def _abs_index(table: TableData):
    for idx in table.indexes:
        if idx.expression:
            compact = idx.expression.upper().replace(" ", "")
            if "ABS(K)" in compact:
                return idx
        if idx.keys == ("k_abs",):
            return idx
    return None


def _partial_threshold(idx_predicate: str) -> Optional[int]:
    m = re.search(r"k\s*<=\s*(\d+)", idx_predicate, re.I)
    return int(m.group(1)) if m else None


def gen_expr_abs_lookup(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Equality on ABS(k) / k_abs against an expression or generated index."""
    del cfg
    tables = [t for t in catalog.ordered_tables() if _abs_index(t) is not None]
    if not tables:
        return None
    table = rng.choice(tables)
    idx = _abs_index(table)
    assert idx is not None
    # k is always positive in generated data, so ABS(k) = k.
    vals = [v for v in table.existing_values("k") if v is not None]
    if not vals:
        return None
    v = rng.choice(vals)
    rows = table.rows_for("k", v)
    if not rows:
        return None
    if idx.keys == ("k_abs",):
        pred = f"k_abs = {sql_literal(v)}"
    else:
        pred = rng.choice(
            [
                f"ABS(k) = {sql_literal(v)}",
                f"abs(k) = {sql_literal(v)}",
            ]
        )
    return QuerySpec(
        path_kind="expr_abs_lookup",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"expression/generated index {idx.name} equality `{pred}`",
        notes=["predicate must match the indexed expression exactly"],
        skip_tightness=True,
    )


def gen_expr_abs_range(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Range on ABS(k) covering a known number of index entries."""
    del cfg
    tables = [t for t in catalog.ordered_tables() if _abs_index(t) is not None]
    if not tables:
        return None
    table = rng.choice(tables)
    idx = _abs_index(table)
    assert idx is not None
    vals = sorted(v for v in table.existing_values("k") if v is not None)
    if len(vals) < 2:
        return None
    hi = rng.choice(vals[1:])
    rows = frozenset().union(*[table.rows_for("k", v) for v in vals if 1 <= int(v) <= int(hi)])
    if not rows:
        return None
    if idx.keys == ("k_abs",):
        pred = f"k_abs >= 1 AND k_abs <= {sql_literal(hi)}"
    else:
        pred = f"ABS(k) <= {sql_literal(hi)}"
    return QuerySpec(
        path_kind="expr_abs_range",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"expression index {idx.name} range `{pred}`; |entries|={len(rows)}",
        notes=["range on the indexed expression must not fall back to a full scan"],
        skip_tightness=True,
    )


def gen_partial_pk_lookup(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Partial INDEX(pk) WHERE k <= T; query must imply the predicate."""
    if cfg.db not in PARTIAL_INDEX_DBS:
        return None
    candidates: List[Tuple[TableData, object, int]] = []
    for table in catalog.ordered_tables():
        for idx in table.partial_indexes():
            thr = _partial_threshold(idx.predicate or "")
            if thr is None or not idx.keys:
                continue
            candidates.append((table, idx, thr))
    if not candidates:
        return None
    table, idx, thr = rng.choice(candidates)
    pk = idx.keys[0]
    # Keys that satisfy the partial predicate.
    eligible = [
        int(row[pk])
        for row in table.rows
        if int(row["k"]) <= thr
    ]
    if len(eligible) < 2:
        return None
    m = rng.randint(1, min(6, len(eligible)))
    keys = rng.sample(eligible, m)
    pred = f"{pk} IN {in_list(keys)} AND k <= {sql_literal(thr)}"
    return QuerySpec(
        path_kind="partial_pk_lookup",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"partial index {idx.name} where k<={thr}; IN of {m} keys",
        notes=["query implies the partial predicate, so the partial index applies"],
    )


def gen_index_merge_or(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """INDEX(a) OR INDEX(b): index-entry upper bound m1+m2 (P1)."""
    if cfg.db not in INDEX_MERGE_DBS:
        return None
    tables = []
    for t in catalog.ordered_tables():
        has_a = any(idx.keys == ("a",) and not idx.unique for idx in t.indexes)
        has_b = any(idx.keys == ("b",) and not idx.unique for idx in t.indexes)
        if has_a and has_b:
            tables.append(t)
    if not tables:
        return None
    table = rng.choice(tables)
    a_vals = [v for v in table.existing_values("a") if v is not None]
    b_vals = [v for v in table.existing_values("b") if v is not None]
    if not a_vals or not b_vals:
        return None
    av = rng.choice(a_vals)
    bv = rng.choice(b_vals)
    rows_a = table.rows_for("a", av)
    rows_b = table.rows_for("b", bv)
    m1, m2 = len(rows_a), len(rows_b)
    if m1 == 0 or m2 == 0:
        return None
    # Index-entry scan bound is the sum; overlap must not shrink it.
    upper = m1 + m2
    if upper >= table.n:
        return None
    pred = f"a = {sql_literal(av)} OR b = {sql_literal(bv)}"
    return QuerySpec(
        path_kind="index_merge_or",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.interval(max(m1, m2), upper),
        comment=f"Index Merge OR on a={av} (|{m1}|) OR b={bv} (|{m2}|); U={upper}",
        notes=[
            "index-entry upper bound is m1+m2 even when result sets overlap",
            "a full table scan of N is a miss",
        ],
        skip_tightness=True,
    )


def gen_covering_lookup(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Covering INDEX(k) INCLUDE (payload): SELECT only indexed+included cols."""
    del cfg
    tables = [t for t in catalog.ordered_tables() if t.covering_indexes()]
    if not tables:
        return None
    table = rng.choice(tables)
    idx = rng.choice(table.covering_indexes())
    vals = [v for v in table.existing_values("k") if v is not None]
    if not vals:
        return None
    v = rng.choice(vals)
    rows = table.rows_for("k", v)
    if not rows:
        return None
    return QuerySpec(
        path_kind="covering_lookup",
        select_list="k, payload",
        from_clause=table.name,
        where=f"k = {sql_literal(v)}",
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"covering index {idx.name}; SELECT k, payload WHERE k={v}",
        notes=["projection is covered; table heap fetch should be avoidable"],
        skip_tightness=True,
    )


def gen_inner_join_elim_fk(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """INNER JOIN with FK from left→right; SELECT left cols only ⇒ right access 0."""
    del cfg
    # Need a child table that declares FK to t0.
    children = [t for t in catalog.ordered_tables() if t.foreign_keys]
    if not children:
        return None
    left = rng.choice(children)
    fk = left.foreign_keys[0]
    right = catalog.table(fk.ref_table)
    lcol, rcol = fk.columns[0], fk.ref_columns[0]
    shared = sorted(set(left.pk_set()) & set(right.pk_set()))
    if len(shared) < 4:
        return None
    m = rng.randint(2, min(8, len(shared)))
    keys = rng.sample(shared, m)
    select_list = ", ".join(
        f"{q(left.name, c.name)}" for c in left.base_columns()[:3]
    )
    from_clause = (
        f"{left.name} INNER JOIN {right.name} "
        f"ON {q(left.name, lcol)} = {q(right.name, rcol)}"
    )
    where = f"{q(left.name, lcol)} IN {in_list(keys)}"
    return QuerySpec(
        path_kind="inner_join_elim_fk",
        select_list=select_list,
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"FK {fk.name}: INNER JOIN unused right {right.name} is eliminable",
        notes=[
            "FK + NOT NULL proves every left key matches; right projection unused",
            "bound is |left filter|; full right scan/hash build is a miss",
        ],
    )


def gen_mv_group_rewrite(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Oracle MV rewrite: GROUP BY k answered from mv_* with |groups| rows."""
    if cfg.db != "oracle":
        return None
    tables = [
        t
        for t in catalog.ordered_tables()
        if t.mv_name and t.mv_cardinality is not None
    ]
    if not tables:
        return None
    table = rng.choice(tables)
    n_mv = int(table.mv_cardinality)
    # Full group-by matches the MV definition exactly.
    return QuerySpec(
        path_kind="mv_group_rewrite",
        select_list="k, COUNT(*) AS cnt",
        from_clause=table.name,
        group_by="k",
        oracle=CardinalityOracle.exact_count(n_mv),
        comment=f"MV {table.mv_name} rewrite; expected access ≤ {n_mv} MV rows",
        notes=[
            "query is answered entirely by the materialized aggregate",
            "a base-table scan of N is a rewrite miss",
        ],
        skip_tightness=True,
    )
