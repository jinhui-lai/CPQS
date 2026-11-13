from __future__ import annotations

import random
from typing import FrozenSet, List, Optional, Tuple

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData, sql_literal
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import QuerySpec
from cpqs.gen.predicates import (
    COL_PRED_KINDS,
    INDEPENDENT_KIND_PAIRS,
    ColFilter,
    pair_values,
    pick_col_int_filter,
    render_composite_pred,
)


def _composite_cols(table: TableData, rng: random.Random) -> Optional[Tuple[str, ...]]:
    idxs = table.composite_unique_indexes()
    if not idxs:
        return None
    keys = rng.choice(idxs).keys
    if keys[-1] == "part" and len(keys) > 2:
        return keys[:-1]
    return keys[:2] if len(keys) >= 2 else None


def _sample_pks(table: TableData, rng: random.Random, k: int) -> List[int]:
    pks = list(table.pk_set())
    if len(pks) < k:
        return []
    return rng.sample(pks, k)


def gen_composite_unique_eq(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    from cpqs.gen.queries import _maybe_having, _pick_table

    table = _pick_table(catalog, rng)
    cols = _composite_cols(table, rng)
    if cols is None:
        return None
    pks = _sample_pks(table, rng, 1)
    if not pks:
        return None
    pk = pks[0]
    pair = table.pair_for_pk(pk, cols)
    if pair is None:
        return None
    pred = " AND ".join(f"{c} = {sql_literal(v)}" for c, v in zip(cols, pair))
    spec = QuerySpec(
        path_kind="composite_unique_eq",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(1, frozenset({pk})),
        comment=f"composite unique equality on {cols}",
    )
    return _maybe_having(spec, rng, cfg)


def gen_composite_unique_in(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    from cpqs.gen.queries import _maybe_having, _pick_table

    table = _pick_table(catalog, rng)
    cols = _composite_cols(table, rng)
    if cols is None:
        return None
    m = rng.randint(2, 6)
    pks = _sample_pks(table, rng, m)
    if len(pks) < m:
        return None
    pairs = pair_values(table, pks, cols)
    if len(pairs) < m:
        return None
    pred = render_composite_pred(table.name, cols, pairs, rng, qualify=False)
    spec = QuerySpec(
        path_kind="composite_unique_in",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(pks), frozenset(pks)),
        comment=f"composite unique IN/OR on {cols}",
    )
    return _maybe_having(spec, rng, cfg)


def gen_composite_unique_prefix(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Leading equality + trailing IN/BETWEEN on a unique pair."""
    from cpqs.gen.queries import _maybe_having, _pick_table

    table = _pick_table(catalog, rng)
    cols = _composite_cols(table, rng)
    if cols is None or len(cols) < 2:
        return None
    lead, trail = cols[0], cols[1]
    lead_vals = [v for v in table.existing_values(lead) if v is not None]
    if not lead_vals:
        return None
    lead_v = rng.choice(lead_vals)
    prefix_pks = table.rows_for(lead, lead_v)
    trail_vals = sorted(
        {
            table.row_by_pk(pk)[trail]
            for pk in prefix_pks
            if table.row_by_pk(pk) is not None
        }
    )
    if len(trail_vals) < 2:
        return None
    span = rng.randint(1, min(6, len(trail_vals)))
    start = rng.randint(0, len(trail_vals) - span)
    chosen = trail_vals[start : start + span]
    rows = frozenset(
        pk
        for pk in prefix_pks
        if (row := table.row_by_pk(pk)) is not None and row[trail] in set(chosen)
    )
    if not rows:
        return None
    pred = render_composite_pred(
        table.name, cols, [(lead_v, v) for v in chosen], rng, qualify=False
    )
    spec = QuerySpec(
        path_kind="composite_unique_prefix",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"composite unique prefix {lead}={lead_v} and {trail} in {chosen}",
    )
    return _maybe_having(spec, rng, cfg)


def independent_pair_pred(
    table: TableData,
    cols: Tuple[str, ...],
    rng: random.Random,
    cfg: Config,
    table_alias: Optional[str] = None,
) -> Optional[Tuple[str, FrozenSet[int], ColFilter, ColFilter]]:
    """Two different predicate forms on a unique pair; oracle is the row intersection."""
    if len(cols) < 2:
        return None
    c0, c1 = cols[0], cols[1]
    max_tuples = max(1, int(table.n * cfg.max_cardinality_ratio))
    kinds = list(COL_PRED_KINDS)
    for _ in range(16):
        if rng.random() < 0.8:
            k0, k1 = rng.choice(INDEPENDENT_KIND_PAIRS)
        else:
            k0, k1 = rng.sample(kinds, 2)
        if k0 == k1:
            continue
        f0 = pick_col_int_filter(table, c0, rng, k0, table_alias=table_alias)
        f1 = pick_col_int_filter(table, c1, rng, k1, table_alias=table_alias)
        if f0 is None or f1 is None:
            continue
        rows = f0.rows & f1.rows
        if not rows or len(rows) > max_tuples:
            continue
        return f"{f0.sql} AND {f1.sql}", rows, f0, f1
    return None


def gen_composite_independent(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Independent IN / BETWEEN / range / = on each column of a unique pair."""
    from cpqs.gen.queries import _maybe_having, _pick_table

    table = _pick_table(catalog, rng)
    cols = _composite_cols(table, rng)
    if cols is None:
        return None
    got = independent_pair_pred(table, cols, rng, cfg)
    if got is None:
        return None
    pred, rows, f0, f1 = got
    spec = QuerySpec(
        path_kind="composite_independent",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"composite unique {cols[0]}:{f0.kind} AND {cols[1]}:{f1.kind}",
        notes=[
            "each column of the unique pair uses a different predicate form; "
            "cardinality is |rows matching both| on generated data"
        ],
    )
    return _maybe_having(spec, rng, cfg)
