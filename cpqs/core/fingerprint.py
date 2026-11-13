from __future__ import annotations

import re
from typing import Optional

from cpqs.core.catalog import Catalog
from cpqs.core.query import QuerySpec

_TABLE_COUNT_RE = re.compile(r"_(?:[234]|n)t$")

# Exact path-kind → family. Schema profile, table width, JOIN keyword, and
# HAVING-vs-WHERE must not create extra reports.
_KIND_FAMILY = {
    "inner_join": "unique_join_chain",
    "straight_join": "unique_join_chain",
    "unique_join": "unique_join_chain",
    "or_of_eq_join": "unique_join_chain",
    "right_join": "left_join",
}

# Prefixes after stripping _Nt / +having. Longer prefixes first.
_KIND_PREFIXES = (
    ("composite_independent_join", "composite_independent_join"),
    ("ordered_limit_join", "ordered_limit_join"),
    ("empty_outer", "empty_outer"),
    ("indep_unique", "indep_unique"),
    ("join_eq_and", "unique_join_chain"),
    ("composite_join", "composite_join"),
    ("eq_and", "unique_eq_and"),
    ("left_join", "left_join"),
    ("join_using", "join_using"),
    ("natural_join", "natural_join"),
    ("check_expr_wrap", "check_expr_wrap"),
    ("check_pair_arith", "check_pair_arith"),
    ("check_pair_point", "check_pair_point"),
    ("check_or_join_empty", "check_or_join_empty"),
    ("check_domain_baseline", "check_domain_baseline"),
    ("partition_out_of_range", "partition_out_of_range"),
    ("partition_bound_hole", "partition_bound_hole"),
    ("partition_expr_prune", "partition_expr_prune"),
    ("partition_expr_empty", "partition_expr_empty"),
    ("partition_list_eq", "partition_list_eq"),
    ("partition_hash_eq", "partition_hash_eq"),
    ("partition_local_index", "partition_local_index"),
    ("null_contradiction", "null_contradiction"),
    ("outer_join_elim", "outer_join_elim"),
    ("desc_ordered_limit", "desc_ordered_limit"),
    ("expr_abs_lookup", "expr_abs_lookup"),
    ("expr_abs_range", "expr_abs_range"),
    ("partial_pk_lookup", "partial_pk_lookup"),
    ("index_merge_or", "index_merge_or"),
    ("covering_lookup", "covering_lookup"),
    ("inner_join_elim_fk", "inner_join_elim_fk"),
    ("mv_group_rewrite", "mv_group_rewrite"),
)

# Generator names whose every spec shares one fingerprint family.
_GENERATOR_KIND_ALIAS = {
    "inner_join_nt": "unique_join_chain",
    "inner_join_3t": "unique_join_chain",
    "inner_join_4t": "unique_join_chain",
    "straight_join_nt": "unique_join_chain",
    "straight_join_3t": "unique_join_chain",
    "straight_join_4t": "unique_join_chain",
    "unique_join": "unique_join_chain",
    "unique_join_eq_and": "unique_join_chain",
    "or_of_eq_join": "unique_join_chain",
    "semi_empty_outer": "empty_outer",
    "independent_unique_filters": "indep_unique",
    "independent_unique_filters_3t": "indep_unique",
    "independent_unique_filters_4t": "indep_unique",
    "outer_join": "left_join",
    "outer_join_chain": "left_join",
    "left_join_3t": "left_join",
    "left_join_4t": "left_join",
    "join_using": "join_using",
    "join_using_3t": "join_using",
    "join_using_4t": "join_using",
    "natural_join": "natural_join",
    "natural_join_3t": "natural_join",
    "natural_join_4t": "natural_join",
    "composite_independent_join": "composite_independent_join",
    "composite_independent_join_3t": "composite_independent_join",
    "composite_independent_join_4t": "composite_independent_join",
    "ordered_limit_join": "ordered_limit_join",
    "check_expr_wrap": "check_expr_wrap",
    "check_pair_arith": "check_pair_arith",
    "check_pair_point": "check_pair_point",
    "check_or_join_empty": "check_or_join_empty",
    "check_domain_baseline": "check_domain_baseline",
    "partition_eq": "partition_eq",
    "partition_range": "partition_range",
    "partition_and_unique": "partition_and_unique",
    "partition_out_of_range": "partition_out_of_range",
    "partition_bound_hole": "partition_bound_hole",
    "partition_expr_prune": "partition_expr_prune",
    "partition_expr_empty": "partition_expr_empty",
    "partition_list_eq": "partition_list_eq",
    "partition_hash_eq": "partition_hash_eq",
    "partition_local_index": "partition_local_index",
    "null_contradiction": "null_contradiction",
    "outer_join_elim": "outer_join_elim",
    "desc_ordered_limit": "desc_ordered_limit",
    "expr_abs_lookup": "expr_abs_lookup",
    "expr_abs_range": "expr_abs_range",
    "partial_pk_lookup": "partial_pk_lookup",
    "index_merge_or": "index_merge_or",
    "covering_lookup": "covering_lookup",
    "inner_join_elim_fk": "inner_join_elim_fk",
    "mv_group_rewrite": "mv_group_rewrite",
}

# One generator emits several fingerprint families (e.g. unique_eq vs unique_in).
_MULTI_KIND_GENERATORS = frozenset(
    {
        "unique_eq_and",
        "semi_join",
        "anti_join",
        "lateral_join",
    }
)


def bug_kind(path_kind: str) -> str:
    """Root-cause key used to de-duplicate PoCs across all DBMS targets.

    ``inner_join_4t`` and ``straight_join_3t`` are the same unique-join
    cardinality bug; ``unique_eq+having`` is the same as ``unique_eq``.
    Distinct SQL constructs (tuple IN join vs INNER JOIN, FULL OUTER, dual
    MIN/MAX) stay separate so each language feature can still be reported once.
    """
    kind = path_kind.split("+", 1)[0]
    kind = _TABLE_COUNT_RE.sub("", kind)
    mapped = _KIND_FAMILY.get(kind)
    if mapped is not None:
        return mapped
    for prefix, family in _KIND_PREFIXES:
        if kind == prefix or kind.startswith(prefix + "_"):
            return family
    return kind


def generator_bug_kind(gen_name: str) -> Optional[str]:
    """Fingerprint family this generator always produces, or None if mixed."""
    if gen_name in _MULTI_KIND_GENERATORS:
        return None
    alias = _GENERATOR_KIND_ALIAS.get(gen_name)
    if alias is not None:
        return alias
    return bug_kind(gen_name)


def bug_fingerprint(catalog: Catalog, spec: QuerySpec) -> str:
    del catalog  # profile / types are not part of the root cause
    return bug_kind(spec.path_kind)
