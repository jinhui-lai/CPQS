from __future__ import annotations

import random
from functools import partial
from typing import Callable, FrozenSet, List, Optional, Tuple

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData, in_list, sql_literal
from cpqs.core.fingerprint import bug_kind, generator_bug_kind
from cpqs.core.oracle import CardinalityOracle, and_oracles, and_residual, or_oracles
from cpqs.core.query import QuerySpec
from cpqs.gen.joins import (
    gen_anti_join,
    gen_composite_independent_join,
    gen_const_range_push,
    gen_cross_join_bounded,
    gen_full_outer_join,
    gen_independent_unique_filters,
    gen_join_using,
    gen_lateral_join,
    gen_multitable_inner,
    gen_natural_join,
    gen_outer_join,
    gen_outer_join_chain,
    gen_semi_empty_outer,
    gen_semi_join,
    gen_semi_join_chain,
    gen_straight_join_chain,
    inner_join_kw,
)
from cpqs.gen.composite import (
    gen_composite_independent,
    gen_composite_unique_eq,
    gen_composite_unique_in,
    gen_composite_unique_prefix,
)
from cpqs.gen.partitions import (
    gen_partition_and_unique,
    gen_partition_bound_hole,
    gen_partition_eq,
    gen_partition_expr_empty,
    gen_partition_expr_prune,
    gen_partition_hash_eq,
    gen_partition_list_eq,
    gen_partition_local_index,
    gen_partition_out_of_range,
    gen_partition_range,
)
from cpqs.gen.predicates import render_join_on
from cpqs.gen.checks import (
    gen_check_domain_baseline,
    gen_check_expr_wrap,
    gen_check_or_join_empty,
    gen_check_pair_arith,
    gen_check_pair_point,
)
from cpqs.gen.constraints import gen_null_contradiction, gen_outer_join_elim
from cpqs.gen.access_ext import (
    gen_covering_lookup,
    gen_expr_abs_lookup,
    gen_expr_abs_range,
    gen_index_merge_or,
    gen_inner_join_elim_fk,
    gen_mv_group_rewrite,
    gen_partial_pk_lookup,
)

Generator = Callable[[Catalog, random.Random, Config], Optional[QuerySpec]]
Filter = Tuple[str, FrozenSet[int], str]

JOIN_GEN_NAMES = frozenset(
    {
        "unique_join_eq_and",
        "inner_join_nt",
        "inner_join_3t",
        "inner_join_4t",
        "straight_join_nt",
        "straight_join_3t",
        "straight_join_4t",
        "join_using",
        "join_using_3t",
        "join_using_4t",
        "natural_join",
        "natural_join_3t",
        "natural_join_4t",
        "outer_join",
        "outer_join_chain",
        "left_join_3t",
        "left_join_4t",
        "semi_join",
        "semi_join_chain",
        "anti_join",
        "lateral_join",
        "cross_join_bounded",
        "full_outer_join",
        "unique_join",
        "tuple_in_join",
        "or_of_eq_join",
        "composite_independent_join",
        "composite_independent_join_3t",
        "composite_independent_join_4t",
        "const_range_push",
        "semi_empty_outer",
        "independent_unique_filters",
        "independent_unique_filters_3t",
        "independent_unique_filters_4t",
        "outer_join_elim",
        "inner_join_elim_fk",
    }
)


def generate_query(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    skip_bug_kinds: Optional[set[str]] = None,
) -> Optional[QuerySpec]:
    skip = skip_bug_kinds or set()
    named = _named_generators(cfg, skip)

    def _try(gens: List[Generator]) -> Optional[QuerySpec]:
        for gen in gens:
            spec = gen(catalog, rng, cfg)
            if spec is None:
                continue
            if bug_kind(spec.path_kind) in skip:
                continue
            n = _bound_n(spec, catalog)
            if spec.oracle.upper_bound > n:
                continue
            if not spec.skip_tightness and not spec.oracle.is_tight(n, cfg.max_cardinality_ratio):
                continue
            return spec
        return None

    join_gens = [g for name, g in named if name in JOIN_GEN_NAMES]
    if join_gens and not cfg.only_kinds and rng.random() < 0.55:
        shuffled = list(join_gens)
        rng.shuffle(shuffled)
        spec = _try(shuffled)
        if spec is not None:
            return spec
    check_gens = [g for name, g in named if name.startswith("check_")]
    if check_gens and not cfg.only_kinds and cfg.db in {"postgres", "opengauss", "oracle"} and rng.random() < 0.25:
        shuffled = list(check_gens)
        rng.shuffle(shuffled)
        spec = _try(shuffled)
        if spec is not None:
            return spec
    part_gens = [g for name, g in named if name.startswith("partition_")]
    if part_gens and not cfg.only_kinds and any(t.partitioned for t in catalog.ordered_tables()) and rng.random() < 0.3:
        shuffled = list(part_gens)
        rng.shuffle(shuffled)
        spec = _try(shuffled)
        if spec is not None:
            return spec
    comp_gens = [g for name, g in named if name.startswith("composite_")]
    if (
        comp_gens
        and not cfg.only_kinds
        and any(t.composite_unique_indexes() for t in catalog.ordered_tables())
        and rng.random() < 0.35
    ):
        shuffled = list(comp_gens)
        rng.shuffle(shuffled)
        spec = _try(shuffled)
        if spec is not None:
            return spec
    all_gens = [g for _, g in named]
    rng.shuffle(all_gens)
    return _try(all_gens)


def _named_generators(
    cfg: Config,
    skip_bug_kinds: Optional[set[str]] = None,
) -> List[tuple[str, Generator]]:
    all_gens: List[tuple[str, Generator]] = [
        ("unique_eq_and", gen_unique_eq_and),
        ("unique_join_eq_and", gen_unique_join_eq_and),
        ("inner_join_nt", gen_multitable_inner),
        ("inner_join_3t", partial(gen_multitable_inner, njoin=3)),
        ("inner_join_4t", partial(gen_multitable_inner, njoin=4)),
        ("straight_join_nt", gen_straight_join_chain),
        ("straight_join_3t", partial(gen_straight_join_chain, njoin=3)),
        ("straight_join_4t", partial(gen_straight_join_chain, njoin=4)),
        ("join_using", gen_join_using),
        ("join_using_3t", partial(gen_join_using, njoin=3)),
        ("join_using_4t", partial(gen_join_using, njoin=4)),
        ("natural_join", gen_natural_join),
        ("natural_join_3t", partial(gen_natural_join, njoin=3)),
        ("natural_join_4t", partial(gen_natural_join, njoin=4)),
        ("outer_join", gen_outer_join),
        ("outer_join_chain", gen_outer_join_chain),
        ("left_join_3t", partial(gen_outer_join_chain, njoin=3)),
        ("left_join_4t", partial(gen_outer_join_chain, njoin=4)),
        ("semi_join", gen_semi_join),
        ("semi_join_chain", gen_semi_join_chain),
        ("anti_join", gen_anti_join),
        ("lateral_join", gen_lateral_join),
        ("cross_join_bounded", gen_cross_join_bounded),
        ("full_outer_join", gen_full_outer_join),
        ("composite_unique_eq", gen_composite_unique_eq),
        ("composite_unique_in", gen_composite_unique_in),
        ("composite_unique_prefix", gen_composite_unique_prefix),
        ("composite_independent", gen_composite_independent),
        ("composite_independent_join", gen_composite_independent_join),
        ("composite_independent_join_3t", partial(gen_composite_independent_join, njoin=3)),
        ("composite_independent_join_4t", partial(gen_composite_independent_join, njoin=4)),
        ("partition_eq", gen_partition_eq),
        ("partition_range", gen_partition_range),
        ("partition_and_unique", gen_partition_and_unique),
        ("partition_out_of_range", gen_partition_out_of_range),
        ("partition_bound_hole", gen_partition_bound_hole),
        ("partition_expr_prune", gen_partition_expr_prune),
        ("partition_expr_empty", gen_partition_expr_empty),
        ("partition_list_eq", gen_partition_list_eq),
        ("partition_hash_eq", gen_partition_hash_eq),
        ("partition_local_index", gen_partition_local_index),
        ("unique_lookup", gen_unique_lookup),
        ("unique_in", gen_unique_in),
        ("physical_lookup", gen_physical_lookup),
        ("nonunique_lookup", gen_nonunique_lookup),
        ("ordered_range", gen_ordered_range),
        ("composite_range", gen_composite_prefix_range),
        ("prefix_order_limit", gen_prefix_order_limit),
        # range_then_eq omitted: small BETWEEN+eq loose spans over-read O(window*|b|),
        # but wall-clock stays ~1–2× vs tight multi-span even at 1M rows (not ≥5×).
        ("tuple_gt", gen_tuple_inequality),
        ("loose_distinct", gen_loose_distinct),
        ("large_in_between", gen_large_in_between),
        ("const_range_push", gen_const_range_push),
        ("semi_empty_outer", gen_semi_empty_outer),
        ("independent_unique_filters", gen_independent_unique_filters),
        ("independent_unique_filters_3t", partial(gen_independent_unique_filters, njoin=3)),
        ("independent_unique_filters_4t", partial(gen_independent_unique_filters, njoin=4)),
        ("boundary_minmax", gen_boundary_minmax),
        ("ordered_limit", gen_ordered_limit),
        ("desc_ordered_limit", gen_desc_ordered_limit),
        ("ordered_limit_extra", gen_ordered_limit_extra),
        ("ordered_limit_join", gen_ordered_limit_join),
        ("ordered_offset", gen_ordered_offset),
        ("null_contradiction", gen_null_contradiction),
        ("outer_join_elim", gen_outer_join_elim),
        ("expr_abs_lookup", gen_expr_abs_lookup),
        ("expr_abs_range", gen_expr_abs_range),
        ("partial_pk_lookup", gen_partial_pk_lookup),
        ("index_merge_or", gen_index_merge_or),
        ("covering_lookup", gen_covering_lookup),
        ("inner_join_elim_fk", gen_inner_join_elim_fk),
        ("mv_group_rewrite", gen_mv_group_rewrite),
        ("and_paths", gen_and_paths),
        ("and_residual", gen_efficient_and_residual),
        ("or_paths", gen_or_paths),
        ("unique_join", gen_unique_equijoin),
        ("tuple_in_join", gen_tuple_in_join),
        ("same_table_tuple_in", gen_same_table_tuple_in),
        ("or_of_eq_join", gen_or_of_eq_join),
        ("dual_minmax", gen_dual_minmax),
        ("unsat_range", gen_unsatisfiable_range),
        ("unsat_eq", gen_unsatisfiable_eq),
        ("check_expr_wrap", gen_check_expr_wrap),
        ("check_pair_arith", gen_check_pair_arith),
        ("check_pair_point", gen_check_pair_point),
        ("check_or_join_empty", gen_check_or_join_empty),
        ("check_domain_baseline", gen_check_domain_baseline),
    ]
    skip = skip_bug_kinds or set()
    out: List[tuple[str, Generator]] = []
    for name, gen in all_gens:
        if name in cfg.skip_kinds:
            continue
        if cfg.only_kinds and name not in cfg.only_kinds:
            continue
        family = generator_bug_kind(name)
        if family is not None and family in skip:
            continue
        out.append((name, gen))
    return out


def _generators(cfg: Config) -> List[Generator]:
    return [g for _, g in _named_generators(cfg)]


def _bound_n(spec: QuerySpec, catalog: Catalog) -> int:
    import re

    blob = " ".join(
        p for p in (spec.from_clause, spec.where, spec.having, spec.select_list) if p
    )
    prod = 1
    found = 0
    for table in catalog.ordered_tables():
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(table.name)}(?![A-Za-z0-9_])", blob):
            prod *= table.n
            found += 1
    if found <= 1:
        return catalog.primary().n
    return prod


def _sample_existing(table: TableData, column: str, rng: random.Random, k: int = 1) -> List[int]:
    values = [v for v in table.existing_values(column) if v is not None]
    if len(values) < k:
        return []
    return rng.sample(values, k)


def _pick_table(catalog: Catalog, rng: random.Random) -> TableData:
    return rng.choice(catalog.ordered_tables())


def _q(table: str, col: str) -> str:
    return f"{table}.{col}"


def _tight_window(table: TableData, col: str, rng: random.Random, cfg: Config) -> Optional[Tuple[int, int, FrozenSet[int]]]:
    ordered = table.ordered_values.get(col, [])
    if len(ordered) < 2:
        return None
    max_tuples = max(1, int(table.n * cfg.max_cardinality_ratio))
    span = rng.randint(1, min(12, len(ordered)))
    start = rng.randint(0, len(ordered) - span)
    low, high = ordered[start], ordered[start + span - 1]
    rows = table.rows_in_range(col, low, high)
    while len(rows) > max_tuples and span > 1:
        span -= 1
        high = ordered[start + span - 1]
        rows = table.rows_in_range(col, low, high)
    if not rows or len(rows) > max_tuples:
        return None
    return int(low), int(high), rows


def _unique_filter(table: TableData, col: str, rng: random.Random, cfg: Config) -> Optional[Filter]:
    """Selective predicate on a unique/ordered column, with known candidate rows."""
    kinds = [
        "eq",
        "in",
        "between",
        "ge_le",
        "gt_lt",
        "gt_le",
        "ge_lt",
        "or_eq",
        "in_and_eq",
        "between_and_eq",
        "eq_redundant",
    ]
    kind = rng.choice(kinds)
    if kind == "eq":
        vals = _sample_existing(table, col, rng, 1)
        if not vals:
            return None
        v = vals[0]
        return f"{col} = {sql_literal(v)}", table.rows_for(col, v), "eq"
    if kind in {"in", "or_eq", "in_and_eq"}:
        m = rng.randint(2, 8)
        vals = _sample_existing(table, col, rng, m)
        if len(vals) < m:
            return None
        rows = frozenset().union(*[table.rows_for(col, v) for v in vals])
        if kind == "or_eq":
            pred = " OR ".join(f"{col} = {sql_literal(v)}" for v in vals)
            return f"({pred})", rows, "or_eq"
        if kind == "in_and_eq":
            v = vals[0]
            pred = f"{col} IN {in_list(vals)} AND {col} = {sql_literal(v)}"
            return pred, table.rows_for(col, v), "in_and_eq"
        return f"{col} IN {in_list(vals)}", rows, "in"
    if kind == "eq_redundant":
        vals = _sample_existing(table, col, rng, 1)
        if not vals:
            return None
        v = vals[0]
        pred = f"{col} = {sql_literal(v)} AND {col} = {sql_literal(v)}"
        return pred, table.rows_for(col, v), "eq_redundant"
    window = _tight_window(table, col, rng, cfg)
    if window is None:
        return None
    low, high, rows = window
    if kind == "between":
        return f"{col} BETWEEN {sql_literal(low)} AND {sql_literal(high)}", rows, "between"
    if kind == "ge_le":
        return f"{col} >= {sql_literal(low)} AND {col} <= {sql_literal(high)}", rows, "ge_le"
    if kind == "between_and_eq":
        v = rng.choice(sorted(rows))
        # v is a pk; the unique col value equals pk because c0=c1=i
        pred = (
            f"{col} BETWEEN {sql_literal(low)} AND {sql_literal(high)} "
            f"AND {col} = {sql_literal(v)}"
        )
        return pred, table.rows_for(col, v), "between_and_eq"
    ordered = table.ordered_values[col]
    lo_i = ordered.index(low)
    hi_i = ordered.index(high)
    if kind == "gt_lt":
        if hi_i - lo_i < 2:
            return None
        inner = table.rows_in_range(col, ordered[lo_i + 1], ordered[hi_i - 1])
        if not inner:
            return None
        return f"{col} > {sql_literal(low)} AND {col} < {sql_literal(high)}", inner, "gt_lt"
    if kind == "gt_le":
        if lo_i + 1 > hi_i:
            return None
        inner = table.rows_in_range(col, ordered[lo_i + 1], high)
        if not inner:
            return None
        return f"{col} > {sql_literal(low)} AND {col} <= {sql_literal(high)}", inner, "gt_le"
    if kind == "ge_lt":
        if lo_i > hi_i - 1:
            return None
        inner = table.rows_in_range(col, low, ordered[hi_i - 1])
        if not inner:
            return None
        return f"{col} >= {sql_literal(low)} AND {col} < {sql_literal(high)}", inner, "ge_lt"
    return None


def _qualify_pred(pred: str, table: str, cols: List[str]) -> str:
    out = pred
    for col in sorted(cols, key=len, reverse=True):
        out = out.replace(col, f"{table}.{col}")
    return out


def _maybe_having(spec: QuerySpec, rng: random.Random, cfg: Config) -> QuerySpec:
    if cfg.db not in {"mysql", "mariadb", "tidb", "oceanbase"}:
        return spec
    if spec.where is None or spec.having is not None or spec.limit is not None:
        return spec
    if "," in spec.from_clause or "JOIN" in spec.from_clause.upper() or "APPLY" in spec.from_clause.upper():
        return spec
    if rng.random() > 0.0001:
        return spec
    spec.having = spec.where
    spec.where = None
    spec.notes.append("predicate placed in HAVING without GROUP BY")
    spec.path_kind = spec.path_kind + "+having"
    return spec


def _maybe_tautology(pred: str, col: str, rng: random.Random, cfg: Config) -> str:
    extras: List[str] = []
    if rng.random() < 0.25:
        extras.append(f"{col} IS NOT NULL")
    if rng.random() < 0.2:
        extras.append("1 = 1")
    if rng.random() < 0.12 and cfg.db in {"mysql", "mariadb", "tidb", "oceanbase"}:
        extras.append("TRUE")
    if extras:
        return pred + " AND " + " AND ".join(extras)
    return pred


def gen_unique_eq_and(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """c0=c1 AND (IN / = / BETWEEN / range) on PRIMARY KEY or UNIQUE INDEX."""
    table = _pick_table(catalog, rng)
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    got = _unique_filter(table, col, rng, cfg)
    if got is None:
        return None
    pred, rows, tag = got
    notes: List[str] = []
    if len(cols) >= 2 and rng.random() < 0.75:
        other = cols[1] if cols[0] == col else cols[0]
        pred = f"{col} = {other} AND {pred}"
        notes.append(f"{col}={other}; both unique; generated rows have {col}={other}")
        tag = "eq_and_" + tag
    pred = _maybe_tautology(pred, col, rng, cfg)
    spec = QuerySpec(
        path_kind="unique_" + tag if not tag.startswith("eq_and_") else tag,
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"unique filter `{pred}`",
        notes=notes,
    )
    return _maybe_having(spec, rng, cfg)


def gen_unique_join_eq_and(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """t0.c0 = t1.c1 AND selective unique predicate; 1-1 unique join."""
    tables = catalog.ordered_tables()
    if len(tables) < 2:
        return None
    t0, t1 = rng.sample(tables, 2)
    left_cols = t0.unique_leading_columns()
    right_cols = t1.unique_leading_columns()
    if not left_cols or not right_cols:
        return None
    left = rng.choice(left_cols)
    right = rng.choice(right_cols)
    got = _unique_filter(t0, left, rng, cfg)
    if got is None:
        return None
    pred, rows, tag = got
    pred = _qualify_pred(pred, t0.name, [left])
    join_pred = render_join_on(t0.name, left, t1.name, right, sorted(rows)[: max(1, len(rows))], rng)
    kw = inner_join_kw(rng, cfg)
    from_clause = f"{t0.name} {kw} {t1.name} ON {join_pred}"
    return QuerySpec(
        path_kind=f"join_eq_and_{tag}",
        select_list="*",
        from_clause=from_clause,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"unique equi-join {join_pred} plus `{pred}`",
        notes=["keys are aligned across tables, so join is 1-1 on matching unique values"],
    )


def gen_unique_lookup(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = _pick_table(catalog, rng)
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    values = _sample_existing(table, col, rng, 1)
    if not values:
        return None
    v = values[0]
    spec = QuerySpec(
        path_kind="unique_lookup",
        select_list="*",
        from_clause=table.name,
        where=f"{col} = {sql_literal(v)}",
        oracle=CardinalityOracle.exact_count(1, table.rows_for(col, v)),
        comment=f"unique lookup on {table.name}.{col}={v}",
    )
    return _maybe_having(spec, rng, cfg)


def gen_unique_in(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = _pick_table(catalog, rng)
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    m = rng.randint(2, 8)
    values = _sample_existing(table, col, rng, m)
    if len(values) < m:
        return None
    rows = frozenset().union(*[table.rows_for(col, v) for v in values])
    spec = QuerySpec(
        path_kind="unique_in",
        select_list="*",
        from_clause=table.name,
        where=f"{col} IN {in_list(values)}",
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"unique IN of {m} existing keys on {col}",
    )
    return _maybe_having(spec, rng, cfg)


def gen_physical_lookup(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    if cfg.db not in {"postgres", "oracle", "opengauss"}:
        return None
    table = catalog.primary()
    pk_col = table.pk_column
    pk = rng.choice(list(table.pk_set()))
    if cfg.db in {"postgres", "opengauss"}:
        return QuerySpec(
            path_kind="physical_lookup",
            select_list="*",
            from_clause=table.name,
            where=f"ctid = (SELECT ctid FROM {table.name} WHERE {pk_col} = {pk})",
            oracle=CardinalityOracle.exact_count(1, frozenset({pk})),
            comment="ctid physical lookup",
        )
    return QuerySpec(
        path_kind="physical_lookup",
        select_list="*",
        from_clause=table.name,
        where=f"ROWID = (SELECT ROWID FROM {table.name} WHERE {pk_col} = {pk})",
        oracle=CardinalityOracle.exact_count(1, frozenset({pk})),
        comment="Oracle ROWID physical lookup",
    )


def gen_nonunique_lookup(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    idx = table.leading_index_for("k")
    if idx is None or idx.unique:
        return None
    values = _sample_existing(table, "k", rng, 1)
    if not values:
        return None
    v = values[0]
    rows = table.rows_for("k", v)
    if not rows:
        return None
    return QuerySpec(
        path_kind="nonunique_lookup",
        select_list="*",
        from_clause=table.name,
        where=f"k = {sql_literal(v)}",
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"non-unique index lookup k={v} multiplicity={len(rows)}",
    )


def gen_ordered_range(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns() + (["k"] if table.leading_index_for("k") else [])
    if not cols:
        return None
    col = rng.choice(cols)
    idx = table.compatible_ordered_index(col)
    if idx is None:
        return None
    window = _tight_window(table, col, rng, cfg)
    if window is None:
        return None
    low, high, rows = window
    form = rng.choice(["between", "ge_le", "gt_lt"])
    if form == "between":
        pred = f"{col} BETWEEN {sql_literal(low)} AND {sql_literal(high)}"
    elif form == "ge_le":
        pred = f"{col} >= {sql_literal(low)} AND {col} <= {sql_literal(high)}"
    else:
        pred = f"{col} > {sql_literal(low - 1)} AND {col} < {sql_literal(high + 1)}"
    return QuerySpec(
        path_kind="ordered_range",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"ordered range {pred} -> {len(rows)} tuples",
    )


def gen_composite_prefix_range(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    idx = table.compatible_ordered_index("b", prefix_eq=("a",))
    if idx is None:
        return None
    a_vals = _sample_existing(table, "a", rng, 1)
    if not a_vals:
        return None
    a = a_vals[0]
    prefix_pks = table.rows_for("a", a)
    b_vals = sorted(
        {table.row_by_pk(pk)["b"] for pk in prefix_pks if table.row_by_pk(pk) is not None}
    )
    if len(b_vals) < 3:
        return None
    span = rng.randint(2, min(8, len(b_vals)))
    start = rng.randint(0, len(b_vals) - span)
    low, high = b_vals[start], b_vals[start + span - 1]
    rows = frozenset(
        pk
        for pk in prefix_pks
        if (row := table.row_by_pk(pk)) is not None and low <= row["b"] <= high
    )
    return QuerySpec(
        path_kind="composite_range",
        select_list="*",
        from_clause=table.name,
        where=f"a = {sql_literal(a)} AND b BETWEEN {sql_literal(low)} AND {sql_literal(high)}",
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"composite (a,b) with a={a} and b in [{low},{high}]",
    )


def _two_col_ordered(table: TableData) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for idx in table.ordered_indexes():
        keys = idx.keys
        if keys and keys[-1] == "part" and len(keys) > 2:
            keys = keys[:-1]
        if len(keys) < 2:
            continue
        pair = (keys[0], keys[1])
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def gen_prefix_order_limit(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Equality on index prefix, ORDER BY the next key, LIMIT k: access is k."""
    table = _pick_table(catalog, rng)
    pairs = _two_col_ordered(table)
    if not pairs:
        return None
    lead, trail = rng.choice(pairs)
    if table.compatible_ordered_index(trail, prefix_eq=(lead,)) is None:
        return None
    lead_vals = [v for v in table.existing_values(lead) if v is not None]
    if not lead_vals:
        return None
    lead_v = rng.choice(lead_vals)
    ordered: List[Tuple[object, int]] = []
    for pk in table.rows_for(lead, lead_v):
        row = table.row_by_pk(pk)
        if row is None:
            continue
        ordered.append((row[trail], pk))
    if not ordered:
        return None
    ordered.sort(key=lambda item: (item[0] is None, item[0], item[1]))
    k = rng.randint(1, min(5, len(ordered)))
    direction = rng.choice(["ASC", "DESC"])
    chosen = ordered[:k] if direction == "ASC" else list(reversed(ordered))[:k]
    rows = frozenset(pk for _, pk in chosen)
    return QuerySpec(
        path_kind="prefix_order_limit",
        select_list="*",
        from_clause=table.name,
        where=f"{lead} = {sql_literal(lead_v)}",
        order_by=f"{trail} {direction}",
        limit=k,
        oracle=CardinalityOracle.exact_count(k, rows),
        comment=f"{lead}={lead_v} ORDER BY {trail} {direction} LIMIT {k}",
        notes=["composite index should satisfy prefix equality and the LIMIT without scanning the whole group"],
    )


def gen_range_then_eq(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Range on the leading index column, equality on the next: opposite of prefix-range.

    Not registered in the default generator list: with typical small windows the
    loose single-span plan over-fetches relative to multi-span, but absolute
    runtime stays far below a 5× gap at million-row scale.
    """
    table = _pick_table(catalog, rng)
    pairs = _two_col_ordered(table)
    if not pairs:
        return None
    lead, trail = rng.choice(pairs)
    ordered_lead = [v for v in table.existing_values(lead) if v is not None]
    if len(ordered_lead) < 4:
        return None
    max_tuples = max(1, int(table.n * cfg.max_cardinality_ratio))
    span = rng.randint(2, min(8, len(ordered_lead) - 1))
    start = rng.randint(0, len(ordered_lead) - span)
    window = ordered_lead[start : start + span]
    lo, hi = window[0], window[-1]
    window_rows = table.rows_in_range(lead, lo, hi)
    trail_vals = sorted(
        {
            table.row_by_pk(pk)[trail]
            for pk in window_rows
            if table.row_by_pk(pk) is not None
        }
    )
    if not trail_vals:
        return None
    trail_v = rng.choice(trail_vals)
    rows = frozenset(
        pk
        for pk in window_rows
        if (row := table.row_by_pk(pk)) is not None and row[trail] == trail_v
    )
    if not rows or len(rows) > max_tuples:
        return None
    form = rng.choice(["between", "ge_le", "gt_lt"])
    if form == "between":
        lead_pred = f"{lead} BETWEEN {sql_literal(lo)} AND {sql_literal(hi)}"
    elif form == "ge_le":
        lead_pred = f"{lead} >= {sql_literal(lo)} AND {lead} <= {sql_literal(hi)}"
    else:
        lead_pred = f"{lead} > {sql_literal(lo - 1)} AND {lead} < {sql_literal(hi + 1)}"
    return QuerySpec(
        path_kind="range_then_eq",
        select_list="*",
        from_clause=table.name,
        where=f"{lead_pred} AND {trail} = {sql_literal(trail_v)}",
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"range on {lead} then equality on {trail}={trail_v}",
        notes=["later key equality after a range should not be left as a residual over the whole range"],
    )


def gen_tuple_inequality(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Row-constructor (c0, c1) > (x, y) plus ORDER BY LIMIT on the unique pair."""
    table = _pick_table(catalog, rng)
    cols = table.unique_leading_columns()
    if len(cols) >= 2:
        c0, c1 = cols[0], cols[1]
    else:
        pairs = _two_col_ordered(table)
        if not pairs:
            return None
        c0, c1 = pairs[0]
    items: List[Tuple[object, object, int]] = []
    for pk in table.pk_set():
        row = table.row_by_pk(pk)
        if row is None:
            continue
        items.append((row[c0], row[c1], pk))
    items.sort(key=lambda t: (t[0] is None, t[0], t[1] is None, t[1], t[2]))
    if len(items) < 12:
        return None
    k = rng.randint(1, 10)
    if rng.random() < 0.5:
        idx = rng.randint(0, len(items) - k - 1)
        x, y, _ = items[idx]
        cand = [pk for _, _, pk in items[idx + 1 : idx + 1 + k]]
        pred = f"({c0}, {c1}) > ({sql_literal(x)}, {sql_literal(y)})"
        order = f"{c0} ASC, {c1} ASC"
    else:
        idx = rng.randint(k, len(items) - 1)
        x, y, _ = items[idx]
        cand = [pk for _, _, pk in items[idx - k : idx]]
        cand = list(reversed(cand))
        pred = f"({c0}, {c1}) < ({sql_literal(x)}, {sql_literal(y)})"
        order = f"{c0} DESC, {c1} DESC"
    if len(cand) < k:
        return None
    return QuerySpec(
        path_kind="tuple_gt",
        select_list="*",
        from_clause=table.name,
        where=pred,
        order_by=order,
        limit=k,
        oracle=CardinalityOracle.exact_count(k, frozenset(cand)),
        comment=f"row-constructor inequality on ({c0},{c1}) LIMIT {k}",
        notes=["tuple > / < on a unique ordered pair is a range; access is LIMIT, not a full index scan"],
    )


def gen_loose_distinct(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """DISTINCT / GROUP BY on a low-cardinality indexed column: |distinct| known."""
    table = catalog.primary()
    if table.leading_index_for("k") is None:
        return None
    distinct = [v for v in table.existing_values("k") if v is not None]
    if not distinct:
        return None
    d = len(distinct)
    if rng.random() < 0.55:
        return QuerySpec(
            path_kind="loose_distinct",
            select_list="DISTINCT k",
            from_clause=table.name,
            oracle=CardinalityOracle.exact_count(d),
            comment=f"SELECT DISTINCT k; |distinct k| = {d}",
            notes=["loose index scan should visit one index entry per distinct value, not N rows"],
        )
    return QuerySpec(
        path_kind="loose_distinct",
        select_list="k, COUNT(*)",
        from_clause=table.name,
        group_by="k",
        oracle=CardinalityOracle.exact_count(d),
        comment=f"GROUP BY k; |distinct k| = {d}",
        notes=["GROUP BY on the leading index column has access cardinality |distinct|"],
    )


def gen_large_in_between(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Long unique IN plus BETWEEN on the same column (eq_range_index_dive_limit)."""
    table = _pick_table(catalog, rng)
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    ordered = [v for v in table.existing_values(col) if v is not None]
    max_tuples = max(1, int(table.n * cfg.max_cardinality_ratio))
    hi_m = min(40, len(ordered), max_tuples)
    if hi_m < 16:
        return None
    m = rng.randint(16, hi_m)
    start = rng.randint(0, len(ordered) - m)
    keys = ordered[start : start + m]
    lo, hi = keys[0], keys[-1]
    rows = table.rows_in_range(col, lo, hi)
    # Contiguous unique keys: IN and BETWEEN name the same set.
    in_rows = frozenset().union(*(table.rows_for(col, v) for v in keys))
    rows = rows & in_rows
    if not rows:
        return None
    spec = QuerySpec(
        path_kind="large_in_between",
        select_list="*",
        from_clause=table.name,
        where=f"{col} IN {in_list(keys)} AND {col} BETWEEN {sql_literal(lo)} AND {sql_literal(hi)}",
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"unique IN of {len(keys)} keys AND BETWEEN on {col}",
        notes=["a long IN list must not drop the same-column BETWEEN and fall back to a table scan"],
    )
    return _maybe_having(spec, rng, cfg)


def gen_boundary_minmax(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    if table.n == 0:
        return None
    cols = table.unique_leading_columns()
    if table.leading_index_for("k"):
        cols = cols + ["k"]
    if not cols:
        return None
    col = rng.choice(cols)
    idx = table.compatible_ordered_index(col)
    if idx is None:
        return None
    fn = rng.choice(["MIN", "MAX"])
    pk = min(table.pk_set()) if fn == "MIN" else max(table.pk_set())
    if col != table.pk_column:
        ordered = table.ordered_values[col]
        target = ordered[0] if fn == "MIN" else ordered[-1]
        pks = table.rows_for(col, target)
        pk = next(iter(pks)) if pks else pk
    return QuerySpec(
        path_kind="boundary_minmax",
        select_list=f"{fn}({col})",
        from_clause=table.name,
        oracle=CardinalityOracle.exact_count(1, frozenset({pk})),
        comment=f"{fn}({col}) via ordered index boundary access",
    )


def gen_ordered_limit(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    idx = table.compatible_ordered_index(col)
    if idx is None:
        return None
    x = rng.randint(1, 20)
    if table.n < x:
        return None
    direction = rng.choice(["ASC", "DESC"])
    pks = sorted(table.pk_set())
    cand = pks[:x] if direction == "ASC" else list(reversed(pks))[:x]
    return QuerySpec(
        path_kind="ordered_limit",
        select_list="*",
        from_clause=table.name,
        order_by=f"{col} {direction}",
        limit=x,
        oracle=CardinalityOracle.exact_count(x, frozenset(cand) if col == table.pk_column else None),
        comment=f"ORDER BY {col} {direction} LIMIT {x}",
    )


def gen_desc_ordered_limit(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """ORDER BY col DESC LIMIT x against an explicit ``INDEX(col DESC)`` (P0 #6)."""
    del cfg
    candidates = []
    for table in catalog.ordered_tables():
        for idx in table.ordered_indexes():
            if not idx.keys or idx.direction_of(idx.keys[0]) != "DESC":
                continue
            candidates.append((table, idx, idx.keys[0]))
    if not candidates:
        return None
    table, idx, col = rng.choice(candidates)
    x = rng.randint(1, 20)
    if table.n < x:
        return None
    # Prefer matching the index direction; occasionally ask for ASC against a
    # DESC index (engines should still backward-scan).
    direction = "DESC" if rng.random() < 0.75 else "ASC"
    if col == table.pk_column:
        pks = sorted(table.pk_set())
        cand = pks[:x] if direction == "ASC" else list(reversed(pks))[:x]
        rows: Optional[FrozenSet[int]] = frozenset(cand)
    else:
        ordered_vals = list(table.ordered_values.get(col, []))
        if direction == "DESC":
            ordered_vals = list(reversed(ordered_vals))
        picked: List[int] = []
        for v in ordered_vals:
            picked.extend(sorted(table.rows_for(col, v)))
            if len(picked) >= x:
                break
        rows = frozenset(picked[:x]) if len(picked) >= x else None
        if rows is None:
            return None
        x = len(rows)
    return QuerySpec(
        path_kind="desc_ordered_limit",
        select_list="*",
        from_clause=table.name,
        order_by=f"{col} {direction}",
        limit=x,
        oracle=CardinalityOracle.exact_count(x, rows),
        comment=f"DESC index {idx.name} ORDER BY {col} {direction} LIMIT {x}",
        notes=["explicit DESC index should satisfy matching Top-K without filesort/full scan"],
    )


def _order_extras(table: TableData, lead: str, rng: random.Random) -> List[str]:
    extras = [c.name for c in table.columns if c.name != lead]
    if not extras:
        return []
    n_extra = rng.randint(1, min(3, len(extras)))
    picked = rng.sample(extras, n_extra)
    return [f"{c} {rng.choice(['ASC', 'DESC'])}" for c in picked]


def gen_ordered_limit_extra(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """ORDER BY unique_col, redundant columns LIMIT k: unique prefix decides the order."""
    table = _pick_table(catalog, rng)
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    idx = table.compatible_ordered_index(col)
    if idx is None:
        return None
    extras = _order_extras(table, col, rng)
    if not extras:
        return None
    x = rng.randint(1, 20)
    y = rng.choice([0, 0, 0, rng.randint(1, 10)])
    if table.n < x + y:
        return None
    direction = rng.choice(["ASC", "DESC"])
    order_by = ", ".join([f"{col} {direction}"] + extras)
    access = x + y
    spec = QuerySpec(
        path_kind="ordered_limit_extra",
        select_list="*",
        from_clause=table.name,
        order_by=order_by,
        limit=x,
        offset=y or None,
        oracle=CardinalityOracle.exact_count(access),
        comment=f"ORDER BY unique {col} plus redundant keys LIMIT {x}"
        + (f" OFFSET {y}" if y else ""),
        notes=["trailing ORDER BY columns cannot change order after a unique leading key"],
    )
    return spec


def gen_ordered_limit_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """1-1 unique join ORDER BY left.pk, inner extra LIMIT k — still [k,k] access."""
    n_avail = len(catalog.ordered_tables())
    njoin = 2 if n_avail < 3 or rng.random() < 0.45 else min(4, n_avail)
    if n_avail < 2:
        return None
    tables = catalog.ordered_tables()
    used = rng.sample(tables, njoin)
    cols = []
    for t in used:
        uc = t.unique_leading_columns()
        if not uc or t.compatible_ordered_index(uc[0]) is None:
            return None
        cols.append(rng.choice(uc))
    t0 = used[0]
    lead = cols[0]
    x = rng.randint(1, 12)
    if min(t.n for t in used) < x:
        return None
    direction = rng.choice(["ASC", "DESC"])
    kw = inner_join_kw(rng, cfg)
    from_clause = t0.name
    for i in range(1, njoin):
        kwi = inner_join_kw(rng, cfg) if rng.random() < 0.5 else kw
        from_clause += (
            f" {kwi} {used[i].name} ON {_q(used[i - 1].name, cols[i - 1])} = {_q(used[i].name, cols[i])}"
        )
    extra_parts = [f"{_q(t0.name, lead)} {direction}"]
    for t, c in zip(used, cols):
        trail = [col.name for col in t.columns if col.name != c]
        if trail:
            extra_parts.append(f"{_q(t.name, rng.choice(trail))} {rng.choice(['ASC', 'DESC'])}")
    where = None
    if rng.random() < 0.55:
        m = rng.randint(max(x, 2), min(x + 8, 16))
        vals = _sample_existing(t0, lead, rng, min(m, t0.n))
        if len(vals) >= x:
            where = f"{_q(t0.name, lead)} IN {in_list(vals[:m] if len(vals) >= m else vals)}"
    return QuerySpec(
        path_kind=f"ordered_limit_join_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=where,
        order_by=", ".join(extra_parts),
        limit=x,
        oracle=CardinalityOracle.exact_count(x),
        comment=f"1-1 unique join ORDER BY {lead} plus redundant columns LIMIT {x}",
        notes=["unique leading ORDER BY on a 1-1 join must stop after LIMIT rows"],
    )


def gen_ordered_offset(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    col = table.pk_column
    idx = table.compatible_ordered_index(col)
    if idx is None:
        return None
    x = rng.randint(1, 10)
    y = rng.randint(1, 30)
    if x + y > table.n:
        return None
    direction = rng.choice(["ASC", "DESC"])
    return QuerySpec(
        path_kind="ordered_offset",
        select_list="*",
        from_clause=table.name,
        order_by=f"{col} {direction}",
        limit=x,
        offset=y,
        oracle=CardinalityOracle.exact_count(x + y),
        comment=f"ORDER BY {col} {direction} LIMIT {x} OFFSET {y}; access={x + y}",
        notes=["access cardinality is limit+offset, result cardinality is limit"],
    )


def gen_and_paths(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    m = rng.randint(2, 5)
    ids = _sample_existing(table, col, rng, m)
    if len(ids) < m:
        return None
    k_vals = _sample_existing(table, "k", rng, 1)
    if not k_vals:
        return None
    k = k_vals[0]
    id_rows = frozenset().union(*[table.rows_for(col, v) for v in ids])
    left = CardinalityOracle.exact_count(len(id_rows), id_rows)
    right = CardinalityOracle.exact_count(table.multiplicity("k", k), table.rows_for("k", k))
    oracle = and_oracles(left, right)
    return QuerySpec(
        path_kind="and_paths",
        select_list="*",
        from_clause=table.name,
        where=f"{col} IN {in_list(ids)} AND k = {sql_literal(k)}",
        oracle=oracle,
        comment="intersection of unique IN and non-unique equality",
    )


def gen_efficient_and_residual(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    ids = _sample_existing(table, col, rng, rng.randint(1, 5))
    if not ids:
        return None
    cand = frozenset().union(*[table.rows_for(col, v) for v in ids])
    efficient = CardinalityOracle.exact_count(len(cand), cand)
    mode = rng.choice(["true", "false", "parity"])
    if mode == "true":
        residual_sql = "payload >= 0"
        residual_rows = cand
    elif mode == "false":
        residual_sql = "payload < 0"
        residual_rows = frozenset()
    else:
        residual_sql = "MOD(payload, 2) = 0"
        residual_rows = frozenset(
            pk for pk in cand if (row := table.row_by_pk(pk)) is not None and row["payload"] % 2 == 0
        )
    oracle = and_residual(efficient, residual_rows)
    return QuerySpec(
        path_kind="and_residual",
        select_list="*",
        from_clause=table.name,
        where=f"{col} IN {in_list(ids)} AND {residual_sql}",
        oracle=oracle,
        comment=f"unique path AND residual `{residual_sql}` evaluated on generated rows",
    )


def gen_or_paths(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    left_ids = _sample_existing(table, col, rng, rng.randint(2, 4))
    right_ids = _sample_existing(table, col, rng, rng.randint(2, 4))
    if not left_ids or not right_ids:
        return None
    left_rows = frozenset().union(*[table.rows_for(col, v) for v in left_ids])
    right_rows = frozenset().union(*[table.rows_for(col, v) for v in right_ids])
    oracle = or_oracles(
        CardinalityOracle.exact_count(len(left_rows), left_rows),
        CardinalityOracle.exact_count(len(right_rows), right_rows),
        table.n,
    )
    if oracle is None:
        return None
    return QuerySpec(
        path_kind="or_paths",
        select_list="*",
        from_clause=table.name,
        where=f"{col} IN {in_list(left_ids)} OR {col} IN {in_list(right_ids)}",
        oracle=oracle,
        comment="union of two unique IN lists",
    )


def _two_tables(catalog: Catalog, rng: random.Random) -> Tuple[TableData, TableData]:
    tables = catalog.ordered_tables()
    if len(tables) < 2:
        return catalog.primary(), catalog.secondary()
    t0, t1 = rng.sample(tables, 2)
    return t0, t1


def _explicit_join(t0: TableData, t1: TableData, on: str, rng: random.Random, cfg: Config) -> str:
    if rng.random() < 0.25:
        return f"{t0.name}, {t1.name}"
    kw = inner_join_kw(rng, cfg)
    return f"{t0.name} {kw} {t1.name} ON {on}"


def gen_unique_equijoin(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    t0, t1 = _two_tables(catalog, rng)
    left = rng.choice(t0.unique_leading_columns() or [t0.pk_column])
    right = rng.choice(t1.unique_leading_columns() or [t1.pk_column])
    m = rng.randint(2, 6)
    shared = sorted(set(t0.pk_set()) & set(t1.pk_set()))
    if len(shared) < m:
        return None
    keys = rng.sample(shared, m)
    on = f"{_q(t0.name, left)} = {_q(t1.name, right)}"
    from_clause = _explicit_join(t0, t1, on, rng, cfg)
    extra = "" if "JOIN" in from_clause.upper() else f"{on} AND "
    return QuerySpec(
        path_kind="unique_join",
        select_list="*",
        from_clause=from_clause,
        where=(
            f"{extra}{_q(t0.name, left)} IN {in_list(keys)} AND {_q(t1.name, right)} IN {in_list(keys)}"
        ),
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"unique equi-join on {m} shared keys",
    )


def gen_tuple_in_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    if rng.random() > 0.01:
        return None
    t0, t1 = _two_tables(catalog, rng)
    left = rng.choice(t0.unique_leading_columns() or [t0.pk_column])
    right = rng.choice(t1.unique_leading_columns() or [t1.pk_column])
    m = rng.randint(2, 4)
    shared = sorted(set(t0.pk_set()) & set(t1.pk_set()))
    if len(shared) < m:
        return None
    keys = rng.sample(shared, m)
    tuples = ", ".join(f"({k}, {k})" for k in keys)
    tuple_pred = f"({_q(t0.name, left)}, {_q(t1.name, right)}) IN ({tuples})"
    if rng.random() < 0.55:
        kw = inner_join_kw(rng, cfg)
        from_clause = f"{t0.name} {kw} {t1.name} ON {tuple_pred}"
        where = None
    else:
        from_clause = f"{t0.name}, {t1.name}"
        where = tuple_pred
    return QuerySpec(
        path_kind="tuple_in_join",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment="row-constructor IN on two unique keys; should not cartesian-product",
        notes=["equivalent to OR of pairwise equality conjunctions"],
    )


def gen_same_table_tuple_in(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns()
    if len(cols) < 2:
        return None
    c0, c1 = cols[0], cols[1]
    m = rng.randint(2, 5)
    vals = _sample_existing(table, c0, rng, m)
    if len(vals) < m:
        return None
    tuples = ", ".join(f"({sql_literal(v)}, {sql_literal(v)})" for v in vals)
    rows = frozenset().union(*[table.rows_for(c0, v) for v in vals])
    spec = QuerySpec(
        path_kind="same_table_tuple_in",
        select_list="*",
        from_clause=table.name,
        where=f"({c0}, {c1}) IN ({tuples})",
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"tuple IN on unique pair ({c0},{c1})",
    )
    return _maybe_having(spec, rng, cfg)


def gen_or_of_eq_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    t0, t1 = _two_tables(catalog, rng)
    left = rng.choice(t0.unique_leading_columns() or [t0.pk_column])
    right = rng.choice(t1.unique_leading_columns() or [t1.pk_column])
    m = rng.randint(2, 4)
    shared = sorted(set(t0.pk_set()) & set(t1.pk_set()))
    if len(shared) < m:
        return None
    keys = rng.sample(shared, m)
    parts = [
        f"({_q(t0.name, left)} = {sql_literal(k)} AND {_q(t1.name, right)} = {sql_literal(k)})" for k in keys
    ]
    on = " OR ".join(parts)
    if rng.random() < 0.5:
        kw = inner_join_kw(rng, cfg)
        from_clause = f"{t0.name} {kw} {t1.name} ON {on}"
        where = None
    else:
        from_clause = f"{t0.name}, {t1.name}"
        where = on
    return QuerySpec(
        path_kind="or_of_eq_join",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment="OR of unique pairwise equalities; positive case vs tuple IN",
    )


def gen_dual_minmax(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    t0, t1 = _two_tables(catalog, rng)
    fn = rng.choice(["MIN", "MAX"])
    left = rng.choice(t0.unique_leading_columns() or [t0.pk_column])
    right = rng.choice(t1.unique_leading_columns() or [t1.pk_column])
    return QuerySpec(
        path_kind="dual_minmax",
        select_list=f"{fn}({_q(t0.name, left)}), {fn}({_q(t1.name, right)})",
        from_clause=f"{t0.name}, {t1.name}",
        oracle=CardinalityOracle.exact_count(1),
        comment=f"{fn} of two independent unique columns must not cartesian-product the tables",
        notes=["each MAX/MIN is a boundary access of cardinality 1"],
    )


def gen_unsatisfiable_range(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    idx = table.compatible_ordered_index(col)
    if idx is None:
        return None
    pred = rng.choice(
        [
            f"{col} > 0 AND {col} < 0",
            f"{col} >= 0 AND {col} < 0",
            f"{col} > 0 AND {col} <= 0",
            f"{col} BETWEEN 10 AND 5",
        ]
    )
    spec = QuerySpec(
        path_kind="unsat_range",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"contradictory ordered range `{pred}`",
    )
    return _maybe_having(spec, rng, cfg)


def gen_unsatisfiable_eq(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    table = catalog.primary()
    cols = table.unique_leading_columns()
    if not cols:
        return None
    col = rng.choice(cols)
    pred = rng.choice(
        [
            f"{col} = 1 AND {col} = 2",
            f"{col} IN (1, 2) AND {col} = 3",
            f"{col} = 1 AND {col} <> 1",
        ]
    )
    spec = QuerySpec(
        path_kind="unsat_eq",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"contradictory unique equality `{pred}`",
    )
    return _maybe_having(spec, rng, cfg)
