"""Bound-aware query mutations.

Every arm returns a candidate only when the pivot still has an independently
derived selective bound. Outcomes are preservation (same bound), tightening
(strictly smaller), or composition (a new bound justified by more than one
key set). Anything else is rejected instead of being given a guessed bound.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData, sql_literal
from cpqs.core.feedback import Parent
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import AccessOccurrence, QuerySpec
from cpqs.gen.access_ext import (
    gen_covering_lookup,
    gen_expr_abs_lookup,
    gen_index_merge_or,
    gen_inner_join_elim_fk,
    gen_partial_pk_lookup,
)
from cpqs.gen.checks import gen_check_domain_baseline
from cpqs.gen.composite import gen_composite_unique_eq
from cpqs.gen.constraints import gen_null_contradiction, gen_outer_join_elim
from cpqs.gen.partitions import gen_partition_eq, gen_partition_hash_eq, gen_partition_list_eq
from cpqs.gen.queries import (
    _bound_n,
    gen_boundary_minmax,
    gen_desc_ordered_limit,
    gen_nonunique_lookup,
    gen_dual_minmax,
    gen_ordered_limit,
    gen_ordered_range,
    gen_unique_in,
    gen_unique_lookup,
    gen_unsatisfiable_eq,
    gen_unsatisfiable_range,
)

QUERY_ARMS: Tuple[str, ...] = (
    "expr_in_to_or",
    "expr_or_to_in",
    "expr_eq_to_in",
    "expr_between_to_cmp",
    "expr_cmp_to_between",
    "expr_shrink",
    "expr_or_compose",
    "expr_join_or",
    "expr_is_not_null",
    "expr_in_to_any",
    "expr_exists_corr",
    "ast_order_limit",
    "ast_literal_proj",
    "ast_derived",
    "ast_cte",
    "ast_in_subquery",
    "ast_union_all",
    "ast_inner_join",
    "ast_full_outer",
    "ast_left_join",
    "ast_distinct",
    "ast_group_by",
    "ast_window",
    "ast_intersect",
    "expr_cast_literal",
    "expr_case_eq",
    "expr_arith_literal",
    "expr_abs_literal",
    "expr_all_single",
    "expr_and_true",
    "expr_having",
)

_ANY_DBS = frozenset({"postgres", "cockroach", "opengauss"})

_FULL_OUTER_DBS = frozenset({"postgres", "cockroach", "oracle", "opengauss"})
_JOIN_DBS = frozenset(
    {"mysql", "mariadb", "tidb", "postgres", "cockroach", "oracle", "opengauss", "oceanbase"}
)
_COL = r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
_INT = r"(-?\d+)"
_EQ = re.compile(rf"^{_COL} = {_INT}$")
_IN = re.compile(rf"^{_COL} IN \(([^)]+)\)$")
_BETWEEN = re.compile(rf"^{_COL} BETWEEN {_INT} AND {_INT}$")
_GELE = re.compile(rf"^{_COL} >= {_INT} AND {_COL} <= {_INT}$")
_JOIN = re.compile(
    rf"^(t\d+) (INNER|FULL OUTER|LEFT) JOIN (t\d+) ON (t\d+)\.(\w+) = (t\d+)\.(\w+)$",
    re.I,
)


@dataclass
class MutationMemory:
    """Per-episode tried targets plus the global normalized-AST cache.

    A cache hit is not a candidate: the caller must not execute it and must
    not charge the query budget.
    """

    tried: set = field(default_factory=set)
    parent_rr: int = 0
    target_rr: Dict[Tuple[str, str], int] = field(default_factory=dict)
    cache: set = field(default_factory=set)

    def reset_episode(self) -> None:
        self.tried.clear()
        self.parent_rr = 0
        self.target_rr.clear()


def ast_fingerprint(spec: QuerySpec, dialect: str) -> str:
    """Normalized SQL: whitespace collapsed and literals replaced."""
    sql = spec.sql(dialect)
    sql = re.sub(r"'(?:''|[^'])*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    sql = re.sub(r"\s+", " ", sql).strip().lower()
    return sql


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _bare(column: str) -> str:
    return column.split(".")[-1]


def _ints(blob: str) -> Optional[List[int]]:
    parts = [part.strip() for part in blob.split(",")]
    values: List[int] = []
    for part in parts:
        if not re.fullmatch(r"-?\d+", part):
            return None
        values.append(int(part))
    return values


def _parse_keys(where: Optional[str]) -> Optional[Tuple[str, List[int], str]]:
    """Return ``(column, values, form)`` for a predicate we can re-derive.

    ``form`` is ``eq``, ``in``, ``or``, ``between``, or ``gele``.
    """
    text = _norm(where)
    if not text:
        return None
    matched = _EQ.match(text)
    if matched:
        return matched.group(1), [int(matched.group(2))], "eq"
    matched = _IN.match(text)
    if matched:
        values = _ints(matched.group(2))
        if values and len(values) == len(set(values)):
            return matched.group(1), values, "in"
    parts = re.split(r" OR ", text)
    if len(parts) >= 2:
        cols: List[str] = []
        values = []
        for part in parts:
            eq = _EQ.match(part.strip())
            if eq is None:
                cols = []
                break
            cols.append(eq.group(1))
            values.append(int(eq.group(2)))
        if cols and len(set(cols)) == 1 and len(values) == len(set(values)):
            return cols[0], values, "or"
    matched = _BETWEEN.match(text)
    if matched:
        low, high = int(matched.group(2)), int(matched.group(3))
        if low <= high:
            return matched.group(1), [low, high], "between"
    matched = _GELE.match(text)
    if matched and matched.group(1) == matched.group(3):
        low, high = int(matched.group(2)), int(matched.group(4))
        if low <= high:
            return matched.group(1), [low, high], "gele"
    return None


def _pivot_table(spec: QuerySpec, catalog: Catalog) -> Optional[TableData]:
    name = spec.pivot_table
    if name and name in catalog.tables:
        return catalog.table(name)
    bare = spec.from_clause.strip()
    if bare in catalog.tables:
        return catalog.table(bare)
    return None


def _rows_for(table: TableData, column: str, values: Sequence[int]):
    found = set()
    for value in values:
        found |= set(table.rows_for(column, value))
    return frozenset(found)


def _indexed(table: TableData, column: str) -> bool:
    return table.leading_index_for(column) is not None


def _ordered(table: TableData, column: str) -> bool:
    return table.compatible_ordered_index(column) is not None


def _selective(spec: QuerySpec, catalog: Catalog, cfg: Config) -> bool:
    if spec.oracle.upper_bound < 0:
        return False
    if spec.skip_tightness:
        return spec.oracle.upper_bound <= _bound_n(spec, catalog)
    width = _bound_n(spec, catalog)
    if spec.oracle.upper_bound > width:
        return False
    return spec.oracle.is_tight(width, cfg.max_cardinality_ratio)


def _finish(
    parent: QuerySpec,
    child: QuerySpec,
    arm: str,
    outcome: str,
    catalog: Catalog,
    cfg: Config,
) -> Optional[QuerySpec]:
    if outcome not in {"preservation", "tightening", "composition"}:
        return None
    parent_bound = parent.oracle.upper_bound
    child_bound = child.oracle.upper_bound
    if outcome == "preservation" and child_bound != parent_bound:
        return None
    if outcome == "tightening" and not (child_bound < parent_bound):
        return None
    if outcome == "composition" and not (child_bound > parent_bound):
        return None
    if parent.oracle.candidate_rows is not None and child.oracle.candidate_rows is not None:
        if outcome == "preservation" and child.oracle.candidate_rows != parent.oracle.candidate_rows:
            return None
        if outcome == "tightening" and not child.oracle.candidate_rows < parent.oracle.candidate_rows:
            return None
    child.path_kind = parent.path_kind
    child.pivot_table = parent.pivot_table or child.pivot_table
    child.pivot_column = parent.pivot_column or child.pivot_column
    child.mutation = arm
    child.bound_outcome = outcome
    child.skip_tightness = parent.skip_tightness and outcome == "preservation"
    child.notes = list(parent.notes) + [f"mutation {arm} ({outcome}) bound {parent_bound}->{child_bound}"]
    if not _selective(child, catalog, cfg):
        return None
    if child.sql(cfg.db) == parent.sql(cfg.db):
        return None
    return _sync_occurrences(child)


def _sync_occurrences(spec: QuerySpec) -> QuerySpec:
    """Keep one pivot bound plus any non-pivot inputs already derived.

    Join composition records the other input before this runs. Tightening
    and preservation refresh the pivot from the rederived oracle and leave
    those other inputs in place.
    """
    if not spec.pivot_table:
        return spec
    extras = tuple(
        occ
        for occ in spec.occurrences
        if not occ.pivot and occ.table != spec.pivot_table
    )
    spec.occurrences = (
        AccessOccurrence(spec.pivot_table, spec.oracle.upper_bound, spec.pivot_column, True),
        *extras,
    )
    return spec


def _clone(parent: QuerySpec, **changes: object) -> QuerySpec:
    from dataclasses import replace

    return replace(parent, notes=list(parent.notes), **changes)


def _single_table(spec: QuerySpec) -> bool:
    if spec.prefix or spec.group_by or spec.having:
        return False
    return re.fullmatch(r"t\d+", spec.from_clause.strip()) is not None


def _other_unique_tables(spec: QuerySpec, catalog: Catalog, parsed) -> List[str]:
    """Other tables whose same column is a unique index, for subquery rewrites."""
    if not _single_table(spec) or parsed is None or parsed[2] not in {"eq", "in", "or"}:
        return []
    table = _pivot_table(spec, catalog)
    if table is None:
        return []
    column = _bare(parsed[0])
    idx = table.leading_index_for(column)
    if idx is None or not idx.unique:
        return []
    names = []
    for other in catalog.ordered_tables():
        if other.name == table.name:
            continue
        other_idx = other.leading_index_for(column)
        if other_idx is not None and other_idx.unique:
            if all(other.rows_for(column, value) for value in parsed[1]):
                names.append(other.name)
    return names


def _targets(arm: str, spec: QuerySpec, catalog: Catalog, cfg: Config) -> List[str]:
    parsed = _parse_keys(spec.where)
    table = _pivot_table(spec, catalog)
    if arm == "expr_in_to_or":
        if parsed and parsed[2] == "in" and len(parsed[1]) >= 2:
            return ["rewrite"]
        return []
    if arm == "expr_or_to_in":
        if parsed and parsed[2] == "or" and len(parsed[1]) >= 2:
            return ["rewrite"]
        return []
    if arm == "expr_eq_to_in":
        if parsed and parsed[2] == "eq":
            return ["rewrite"]
        return []
    if arm == "expr_between_to_cmp":
        if parsed and parsed[2] == "between":
            return ["rewrite"]
        return []
    if arm == "expr_cmp_to_between":
        if parsed and parsed[2] == "gele":
            return ["rewrite"]
        return []
    if arm == "expr_shrink":
        if parsed is None or parsed[2] not in {"in", "or"} or table is None:
            return []
        column, values, _form = parsed
        if not _indexed(table, _bare(column)):
            return []
        if len(values) < 2:
            return []
        return [str(value) for value in values]
    if arm == "expr_or_compose":
        if not _single_table(spec) or parsed is None or table is None:
            return []
        if parsed[2] not in {"eq", "in", "or"}:
            return []
        column = _bare(parsed[0])
        if not _indexed(table, column):
            return []
        have = set(parsed[1])
        extra = [value for value in table.existing_values(column) if value not in have and value is not None]
        return [str(value) for value in extra[:6]]
    if arm == "expr_join_or":
        return _join_or_targets(spec, catalog)
    if arm == "ast_order_limit":
        if not _single_table(spec) or spec.limit is not None or spec.order_by is not None:
            return []
        if parsed is None or table is None or parsed[2] not in {"eq", "in", "or", "between", "gele"}:
            return []
        column = _bare(parsed[0])
        if not _ordered(table, column):
            return []
        width = spec.oracle.upper_bound
        if width < 2:
            return []
        caps = [f"{k}:0" for k in (1, 2, 3) if k < width]
        # OFFSET examines limit+offset ordered entries, still inside the predicate.
        if width > 2:
            caps.append("1:1")
        return caps
    if arm == "ast_literal_proj":
        # Only a star projection of an already restricted access. Replacing
        # MIN/MAX or an unrestricted SELECT * would drop the proof.
        if spec.select_list.strip() != "*":
            return []
        if spec.group_by or spec.having:
            return []
        if spec.where is None and spec.limit is None:
            return []
        return ["lit"]
    if arm == "ast_derived":
        if spec.oracle.upper_bound <= 0 or not _single_table(spec):
            return []
        if "(" in spec.from_clause:
            return []
        return ["wrap"]
    if arm == "ast_cte":
        if spec.oracle.upper_bound <= 0 or not _single_table(spec) or spec.prefix:
            return []
        return ["wrap"]
    if arm == "expr_is_not_null":
        if parsed is None or table is None or parsed[2] not in {"eq", "in", "or", "between", "gele"}:
            return []
        column = _bare(parsed[0])
        if column not in table.not_null_columns():
            return []
        if spec.where and "IS NOT NULL" in spec.where.upper():
            return []
        return ["notnull"]
    if arm == "expr_in_to_any":
        if cfg.db not in _ANY_DBS or parsed is None or parsed[2] not in {"eq", "in", "or"}:
            return []
        return ["rewrite"]
    if arm == "expr_exists_corr":
        return _other_unique_tables(spec, catalog, parsed)
    if arm == "ast_in_subquery":
        return _other_unique_tables(spec, catalog, parsed)
    if arm == "ast_union_all":
        if not _single_table(spec) or parsed is None or parsed[2] not in {"in", "or"}:
            return []
        if len(parsed[1]) < 2:
            return []
        return ["split"]
    if arm == "ast_inner_join":
        if cfg.db not in _JOIN_DBS or not _single_table(spec) or parsed is None or table is None:
            return []
        if parsed[2] not in {"eq", "in", "or"}:
            return []
        # Window ORDER BY / PARTITION BY keep bare names; joining would make
        # them ambiguous or unknown (TiDB 1054, Oracle ORA-00918).
        if "OVER (" in (spec.select_list or "").upper():
            return []
        column = _bare(parsed[0])
        if table.leading_index_for(column) is None or not table.leading_index_for(column).unique:
            return []
        names = []
        for other in catalog.ordered_tables():
            if other.name == table.name:
                continue
            idx = other.leading_index_for(column)
            if idx is not None and idx.unique:
                names.append(other.name)
        return names
    if arm == "ast_full_outer":
        if cfg.db not in _FULL_OUTER_DBS:
            return []
        return _targets("ast_inner_join", spec, catalog, cfg)
    if arm == "ast_left_join":
        if cfg.db not in _JOIN_DBS:
            return []
        return _targets("ast_inner_join", spec, catalog, cfg)
    extra = _extra_targets(arm, spec, catalog, cfg)
    if extra is not None:
        return extra
    return []


def _join_or_targets(spec: QuerySpec, catalog: Catalog) -> List[str]:
    matched = _JOIN.match(_norm(spec.from_clause))
    parsed = _parse_keys(spec.where)
    if matched is None or parsed is None or parsed[2] not in {"eq", "in"}:
        return []
    # Join-elimination seeds prove the right input is unused. An OR that
    # names the right table reintroduces that input and drops the proof.
    if "elim" in (spec.path_kind or ""):
        return []
    left, _kind, right, on_l, on_lc, on_r, on_rc = matched.groups()
    if on_l != left or on_r != right or on_lc != on_rc:
        return []
    column = on_lc
    where_col = parsed[0]
    if where_col not in {f"{left}.{column}", column}:
        return []
    if left not in catalog.tables or right not in catalog.tables:
        return []
    other = catalog.table(right)
    if other.leading_index_for(column) is None or not other.leading_index_for(column).unique:
        return []
    have = set(parsed[1])
    extra = [value for value in other.existing_values(column) if value not in have and value is not None]
    return [str(value) for value in extra[:4]]


def _apply(arm: str, spec: QuerySpec, target: str, catalog: Catalog, cfg: Config) -> Optional[QuerySpec]:
    parsed = _parse_keys(spec.where)
    table = _pivot_table(spec, catalog)
    if arm == "expr_in_to_or" and parsed and parsed[2] == "in":
        column, values, _form = parsed
        pred = " OR ".join(f"{column} = {sql_literal(value)}" for value in values)
        child = _clone(spec, where=pred, comment=spec.comment + " | IN rewritten to OR")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_or_to_in" and parsed and parsed[2] == "or":
        column, values, _form = parsed
        pred = f"{column} IN ({', '.join(sql_literal(value) for value in values)})"
        child = _clone(spec, where=pred, comment=spec.comment + " | OR rewritten to IN")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_eq_to_in" and parsed and parsed[2] == "eq":
        column, values, _form = parsed
        pred = f"{column} IN ({sql_literal(values[0])})"
        child = _clone(spec, where=pred, comment=spec.comment + " | equality rewritten to IN")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_between_to_cmp" and parsed and parsed[2] == "between":
        column, values, _form = parsed
        pred = f"{column} >= {sql_literal(values[0])} AND {column} <= {sql_literal(values[1])}"
        child = _clone(spec, where=pred, comment=spec.comment + " | BETWEEN rewritten to comparisons")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_cmp_to_between" and parsed and parsed[2] == "gele":
        column, values, _form = parsed
        pred = f"{column} BETWEEN {sql_literal(values[0])} AND {sql_literal(values[1])}"
        child = _clone(spec, where=pred, comment=spec.comment + " | comparisons rewritten to BETWEEN")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_shrink" and parsed and table is not None and parsed[2] in {"in", "or"}:
        column, values, form = parsed
        drop = int(target)
        if drop not in values:
            return None
        kept = [value for value in values if value != drop]
        rows = _rows_for(table, _bare(column), kept)
        if not rows or len(rows) >= spec.oracle.upper_bound:
            return None
        if form == "or":
            pred = " OR ".join(f"{column} = {sql_literal(value)}" for value in kept)
        else:
            pred = f"{column} IN ({', '.join(sql_literal(value) for value in kept)})"
        child = _clone(
            spec,
            where=pred,
            oracle=CardinalityOracle.exact_count(len(rows), rows),
            comment=spec.comment + f" | dropped key {drop}",
            pivot_column=_bare(column),
        )
        return _finish(spec, child, arm, "tightening", catalog, cfg)
    if arm == "expr_or_compose" and parsed and table is not None and _single_table(spec):
        column, values, _form = parsed
        extra = int(target)
        if extra in values:
            return None
        bare = _bare(column)
        added = table.rows_for(bare, extra)
        if not added:
            return None
        merged_values = list(values) + [extra]
        rows = _rows_for(table, bare, merged_values)
        if len(rows) <= spec.oracle.upper_bound:
            return None
        pred = " OR ".join(f"{column} = {sql_literal(value)}" for value in merged_values)
        child = _clone(
            spec,
            where=pred,
            oracle=CardinalityOracle.exact_count(len(rows), rows),
            comment=spec.comment + f" | disjunct {bare}={extra}",
            pivot_column=bare,
        )
        return _finish(spec, child, arm, "composition", catalog, cfg)
    if arm == "expr_join_or":
        return _apply_join_or(spec, target, catalog, cfg)
    if arm == "expr_is_not_null" and parsed is not None and target == "notnull":
        pred = f"({spec.where}) AND {parsed[0]} IS NOT NULL"
        child = _clone(spec, where=pred, comment=spec.comment + " | redundant IS NOT NULL")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_in_to_any" and parsed is not None and parsed[2] in {"eq", "in", "or"}:
        column, values, _form = parsed
        array = ", ".join(sql_literal(value) for value in values)
        pred = f"{column} = ANY (ARRAY[{array}])"
        child = _clone(spec, where=pred, comment=spec.comment + " | IN rewritten to ANY")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm in {"expr_exists_corr", "ast_in_subquery"} and parsed is not None and table is not None:
        return _apply_subquery(arm, spec, target, catalog, cfg, parsed, table)
    if arm == "ast_union_all" and parsed is not None and table is not None and target == "split":
        return _apply_union(spec, catalog, cfg, parsed, table)
    if arm == "ast_order_limit":
        return _apply_order_limit(spec, target, catalog, cfg, parsed, table)
    if arm == "ast_literal_proj" and target == "lit":
        child = _clone(
            spec,
            select_list="1 AS cpqs_lit",
            comment=spec.comment + " | extra literal projection",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "ast_derived" and target == "wrap":
        inner = spec.sql(cfg.db)
        alias = "s0" if cfg.db == "oracle" else "AS s0"
        child = _clone(
            spec,
            select_list="*",
            from_clause=f"({inner}) {alias}",
            where=None,
            group_by=None,
            having=None,
            order_by=None,
            limit=None,
            offset=None,
            prefix="",
            comment=spec.comment + " | derived-table wrapper",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "ast_cte" and target == "wrap":
        inner = spec.sql(cfg.db)
        child = _clone(
            spec,
            prefix=f"WITH s0 AS ({inner})",
            select_list="*",
            from_clause="s0",
            where=None,
            group_by=None,
            having=None,
            order_by=None,
            limit=None,
            offset=None,
            comment=spec.comment + " | CTE wrapper",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm in {"ast_inner_join", "ast_full_outer", "ast_left_join"}:
        return _apply_join(arm, spec, target, catalog, cfg, parsed, table)
    return _apply_extra(arm, spec, target, catalog, cfg, parsed, table)


def _apply_order_limit(spec, target, catalog, cfg, parsed, table) -> Optional[QuerySpec]:
    if parsed is None or table is None:
        return None
    limit_s, offset_s = target.split(":")
    limit_n, offset_n = int(limit_s), int(offset_s)
    examined = limit_n + offset_n
    if examined <= 0 or examined >= spec.oracle.upper_bound:
        return None
    column = _bare(parsed[0])
    if not _ordered(table, column):
        return None
    rows = spec.oracle.candidate_rows
    if rows is not None and len(rows) < examined:
        return None
    prefix = _ordered_prefix(table, column, rows, examined) if rows is not None else None
    if prefix is not None and len(prefix) != examined:
        return None
    child = _clone(
        spec,
        order_by=f"{parsed[0]} ASC",
        limit=limit_n,
        offset=offset_n or None,
        oracle=CardinalityOracle.exact_count(examined, prefix if prefix is not None else rows),
        comment=spec.comment + f" | ORDER BY {column} LIMIT {limit_n} OFFSET {offset_n}",
        pivot_column=column,
    )
    return _finish(spec, child, "ast_order_limit", "tightening", catalog, cfg)


def _ordered_prefix(table: TableData, column: str, rows, k: int):
    decorated = []
    for pk in rows:
        row = table.row_by_pk(int(pk))
        if row is None or column not in row:
            return None
        decorated.append((row[column], int(pk)))
    decorated.sort()
    return frozenset(pk for _value, pk in decorated[:k])


def _apply_join(arm, spec, target, catalog, cfg, parsed, table) -> Optional[QuerySpec]:
    if parsed is None or table is None or target not in catalog.tables:
        return None
    if not _single_table(spec):
        return None
    column = _bare(parsed[0])
    other = catalog.table(target)
    values = parsed[1]
    other_rows = _rows_for(other, column, values)
    if len(other_rows) != len(values):
        return None
    bound = max(spec.oracle.upper_bound, len(other_rows))
    if bound != spec.oracle.upper_bound:
        return None
    qualified = parsed[0] if "." in parsed[0] else f"{table.name}.{column}"
    if parsed[2] == "eq":
        pred = f"{qualified} = {sql_literal(values[0])}"
    else:
        pred = f"{qualified} IN ({', '.join(sql_literal(value) for value in values)})"
    keyword = {"ast_full_outer": "FULL OUTER JOIN", "ast_left_join": "LEFT JOIN"}.get(arm, "INNER JOIN")
    # Qualify the projection/grouping so both tables' shared column names do
    # not raise "column ambiguously specified" (Oracle ORA-00918, etc.).
    select_list = _qualify_projection(spec.select_list, table.name)
    group_by = _qualify_projection(spec.group_by, table.name) if spec.group_by else None
    order_by = _qualify_projection(spec.order_by, table.name) if spec.order_by else None
    # Each input is probed on the same unique keys. The pivot scalar stays
    # ``bound`` (preservation); the second occurrence is the other input.
    child = _clone(
        spec,
        select_list=select_list,
        from_clause=f"{table.name} {keyword} {other.name} ON {table.name}.{column} = {other.name}.{column}",
        where=pred,
        group_by=group_by,
        order_by=order_by,
        oracle=CardinalityOracle.exact_count(bound, spec.oracle.candidate_rows),
        comment=spec.comment + f" | {keyword} {other.name}",
        pivot_table=table.name,
        pivot_column=column,
        occurrences=(
            AccessOccurrence(table.name, bound, column, True),
            AccessOccurrence(other.name, len(other_rows), column, False),
        ),
    )
    return _finish(spec, child, arm, "preservation", catalog, cfg)


def _qualify_projection(text: Optional[str], table: str) -> str:
    """Attach ``table.`` to bare column names; leave ``*``, aliases, and calls alone."""
    if text is None:
        return "*"
    body = text.strip()
    if not body:
        return body
    if body == "*":
        return f"{table}.*"
    # DISTINCT <expr>
    distinct = ""
    if body.upper().startswith("DISTINCT "):
        distinct = "DISTINCT "
        body = body[9:].strip()

    def qualify_token(token: str) -> str:
        raw = token.strip()
        if not raw or raw == "*":
            return f"{table}.*" if raw == "*" else raw
        # Keep ``t0.c0``, function calls, and ``expr AS alias``.
        if "(" in raw or "." in raw.split()[0]:
            return raw
        parts = re.split(r"\s+AS\s+", raw, maxsplit=1, flags=re.I)
        head = parts[0].strip()
        alias = f" AS {parts[1].strip()}" if len(parts) == 2 else ""
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head):
            return f"{table}.{head}{alias}"
        return raw

    pieces = [qualify_token(piece) for piece in body.split(",")]
    return distinct + ", ".join(pieces)


def _apply_subquery(arm, spec, target, catalog, cfg, parsed, table) -> Optional[QuerySpec]:
    if target not in catalog.tables or target == table.name:
        return None
    column = _bare(parsed[0])
    other = catalog.table(target)
    values = parsed[1]
    if any(not other.rows_for(column, value) for value in values):
        return None
    listed = ", ".join(sql_literal(value) for value in values)
    if len(values) == 1:
        key_pred = f"{target}.{column} = {sql_literal(values[0])}"
    else:
        key_pred = f"{target}.{column} IN ({listed})"
    if arm == "ast_in_subquery":
        pred = f"{table.name}.{column} IN (SELECT {target}.{column} FROM {target} WHERE {key_pred})"
    else:
        pred = (
            f"({spec.where}) AND EXISTS (SELECT 1 FROM {target} "
            f"WHERE {target}.{column} = {table.name}.{column})"
        )
    child = _clone(
        spec,
        where=pred,
        comment=spec.comment + f" | {arm} via {target}",
        pivot_table=table.name,
        pivot_column=column,
    )
    return _finish(spec, child, arm, "preservation", catalog, cfg)


def _apply_union(spec, catalog, cfg, parsed, table) -> Optional[QuerySpec]:
    values = list(parsed[1])
    if len(values) < 2:
        return None
    column = parsed[0]
    head, tail = values[0], values[1:]
    if len(tail) == 1:
        right = f"{column} = {sql_literal(tail[0])}"
    else:
        right = f"{column} IN ({', '.join(sql_literal(value) for value in tail)})"
    left = f"{column} = {sql_literal(head)}"
    alias = "s0" if cfg.db == "oracle" else "AS s0"
    union_sql = (
        f"(SELECT * FROM {table.name} WHERE {left} "
        f"UNION ALL SELECT * FROM {table.name} WHERE {right}) {alias}"
    )
    child = _clone(
        spec,
        select_list="*",
        from_clause=union_sql,
        where=None,
        order_by=None,
        limit=None,
        offset=None,
        comment=spec.comment + " | UNION ALL split of the same keys",
        pivot_table=table.name,
        pivot_column=_bare(column),
    )
    return _finish(spec, child, "ast_union_all", "preservation", catalog, cfg)


def _apply_join_or(spec: QuerySpec, target: str, catalog: Catalog, cfg: Config) -> Optional[QuerySpec]:
    matched = _JOIN.match(_norm(spec.from_clause))
    parsed = _parse_keys(spec.where)
    if matched is None or parsed is None:
        return None
    left, _kind, right, _on_l, column, _on_r, on_rc = matched.groups()
    if column != on_rc or left not in catalog.tables or right not in catalog.tables:
        return None
    extra = int(target)
    have = list(parsed[1])
    if extra in have:
        return None
    left_table = catalog.table(left)
    right_table = catalog.table(right)
    union_values = have + [extra]
    left_rows = _rows_for(left_table, column, union_values)
    right_rows = _rows_for(right_table, column, union_values)
    if len(left_rows) != len(union_values) or len(right_rows) != len(union_values):
        return None
    bound = len(union_values)
    if bound <= spec.oracle.upper_bound:
        return None
    pred = (
        f"({left}.{column} IN ({', '.join(sql_literal(value) for value in have)}) "
        f"OR {right}.{column} IN ({sql_literal(extra)}))"
    )
    child = _clone(
        spec,
        where=pred,
        oracle=CardinalityOracle.exact_count(bound, left_rows),
        comment=spec.comment + f" | OR {right}.{column}={extra}",
        pivot_table=left,
        pivot_column=column,
        occurrences=(
            AccessOccurrence(left, bound, column, True),
            AccessOccurrence(right, len(right_rows), column, False),
        ),
    )
    # The parent candidate rows are a subset of the expanded key set, so the
    # preservation/tightening subset check does not apply. Composition only
    # requires the new bound to be the justified union size.
    child.notes = list(spec.notes)
    outcome_parent = _clone(spec, oracle=CardinalityOracle.exact_count(spec.oracle.upper_bound, None))
    return _finish(outcome_parent, child, "expr_join_or", "composition", catalog, cfg)


def _select_is_plain(spec: QuerySpec) -> bool:
    text = spec.select_list.upper()
    blocked = ("MIN(", "MAX(", "COUNT(", "SUM(", "AVG(", "DISTINCT", "ROW_NUMBER", "OVER (")
    return not any(token in text for token in blocked)


def _shape_ready(spec: QuerySpec, catalog: Catalog) -> bool:
    """Single-table indexed predicate whose access bound can be restated."""
    if not _single_table(spec):
        return False
    if spec.limit is not None or spec.order_by or spec.group_by or spec.having or spec.prefix:
        return False
    if spec.oracle.upper_bound <= 0:
        return False
    parsed = _parse_keys(spec.where)
    if parsed is None:
        return False
    table = _pivot_table(spec, catalog)
    if table is None or not _indexed(table, _bare(parsed[0])):
        return False
    return True


def _literal_wrap(dialect: str, value: int, how: str) -> Optional[str]:
    """Rewrite a constant. The access column stays bare, so the bound holds."""
    literal = sql_literal(value)
    if how == "arith":
        return f"{literal} + 0"
    if how == "abs":
        if value < 0:
            return None
        return f"ABS({literal})"
    if how == "cast":
        if dialect in {"mysql", "mariadb", "tidb", "oceanbase"}:
            return f"CAST({literal} AS SIGNED)"
        if dialect == "oracle":
            return f"CAST({literal} AS NUMBER)"
        if dialect == "clickhouse":
            return f"CAST({literal} AS Int64)"
        return f"CAST({literal} AS BIGINT)"
    return None


def _pred_with_literal(column: str, values: Sequence[int], form: str, dialect: str, how: str) -> Optional[str]:
    wrapped: List[str] = []
    for value in values:
        piece = _literal_wrap(dialect, value, how)
        if piece is None:
            return None
        wrapped.append(piece)
    if form == "eq":
        return f"{column} = {wrapped[0]}"
    if form == "or":
        return " OR ".join(f"{column} = {piece}" for piece in wrapped)
    if form == "in":
        return f"{column} IN ({', '.join(wrapped)})"
    if form == "between":
        return f"{column} BETWEEN {wrapped[0]} AND {wrapped[1]}"
    if form == "gele":
        return f"{column} >= {wrapped[0]} AND {column} <= {wrapped[1]}"
    return None


def _extra_targets(arm: str, spec: QuerySpec, catalog: Catalog, cfg: Config) -> Optional[List[str]]:
    """Targets for the AST and expression arms added from the design section.

    ``None`` means this arm is not one of those operators.
    """
    known = {
        "ast_distinct",
        "ast_group_by",
        "ast_window",
        "ast_intersect",
        "expr_cast_literal",
        "expr_case_eq",
        "expr_arith_literal",
        "expr_abs_literal",
        "expr_all_single",
        "expr_and_true",
        "expr_having",
    }
    if arm not in known:
        return None
    if arm == "expr_having":
        # HAVING is legal only after GROUP BY has already been introduced.
        if not spec.group_by or spec.having or not spec.where:
            return []
        if _parse_keys(spec.where) is None:
            return []
        return ["copy"]
    if not _shape_ready(spec, catalog):
        return []
    parsed = _parse_keys(spec.where)
    if parsed is None:
        return []
    form = parsed[2]
    if arm in {"ast_distinct", "ast_group_by", "ast_window"}:
        if not _select_is_plain(spec) or form not in {"eq", "in", "or", "between", "gele"}:
            return []
        return ["shape"]
    if arm == "ast_intersect":
        if form not in {"eq", "in", "or", "between", "gele"}:
            return []
        return ["twice"]
    if arm == "expr_all_single":
        if cfg.db not in _ANY_DBS or form != "eq":
            return []
        return ["rewrite"]
    if arm == "expr_case_eq":
        if form != "eq":
            return []
        return ["rewrite"]
    if arm == "expr_abs_literal":
        if any(value < 0 for value in parsed[1]):
            return []
        return ["rewrite"]
    if arm in {"expr_cast_literal", "expr_arith_literal", "expr_and_true"}:
        return ["rewrite"]
    return []


def _apply_extra(arm, spec, target, catalog, cfg, parsed, table) -> Optional[QuerySpec]:
    if arm == "expr_having" and target == "copy" and spec.group_by and spec.where and not spec.having:
        child = _clone(
            spec,
            having=spec.where,
            comment=spec.comment + " | HAVING repeats the access predicate",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if parsed is None or table is None or not _shape_ready(spec, catalog):
        return None
    column, values, form = parsed
    bare = _bare(column)
    if arm == "ast_distinct" and target == "shape":
        child = _clone(
            spec,
            select_list=f"DISTINCT {bare}",
            comment=spec.comment + " | DISTINCT on the access key",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "ast_group_by" and target == "shape":
        child = _clone(
            spec,
            select_list=f"{bare}, COUNT(*) AS cpqs_n",
            group_by=bare,
            comment=spec.comment + " | GROUP BY the access key",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "ast_window" and target == "shape":
        child = _clone(
            spec,
            select_list=f"{bare}, ROW_NUMBER() OVER (ORDER BY {bare} ASC) AS cpqs_rn",
            comment=spec.comment + " | window over the access key",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "ast_intersect" and target == "twice":
        inner = spec.sql(cfg.db)
        alias = "s0" if cfg.db == "oracle" else "AS s0"
        # Both branches examine the key set. Repeated probes add, they do not cancel.
        examined = spec.oracle.upper_bound * 2
        child = _clone(
            spec,
            select_list="*",
            from_clause=f"({inner} INTERSECT {inner}) {alias}",
            where=None,
            order_by=None,
            limit=None,
            offset=None,
            oracle=CardinalityOracle.exact_count(examined, spec.oracle.candidate_rows),
            comment=spec.comment + " | INTERSECT examines the key set twice",
            pivot_column=bare,
        )
        return _finish(spec, child, arm, "composition", catalog, cfg)
    if arm == "expr_and_true" and target == "rewrite":
        child = _clone(
            spec,
            where=f"({spec.where}) AND 1 = 1",
            comment=spec.comment + " | tautology AND 1 = 1",
        )
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_case_eq" and target == "rewrite" and form == "eq":
        value = sql_literal(values[0])
        pred = f"{column} = CASE WHEN 1 = 1 THEN {value} ELSE {value} END"
        child = _clone(spec, where=pred, comment=spec.comment + " | CASE around the literal")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    if arm == "expr_all_single" and target == "rewrite" and form == "eq" and cfg.db in _ANY_DBS:
        pred = f"{column} = ALL (ARRAY[{sql_literal(values[0])}])"
        child = _clone(spec, where=pred, comment=spec.comment + " | equality rewritten to ALL")
        return _finish(spec, child, arm, "preservation", catalog, cfg)
    how = {"expr_cast_literal": "cast", "expr_arith_literal": "arith", "expr_abs_literal": "abs"}.get(arm)
    if how is None or target != "rewrite":
        return None
    pred = _pred_with_literal(column, values, form, cfg.db, how)
    if pred is None:
        return None
    child = _clone(spec, where=pred, comment=spec.comment + f" | literal {how}")
    return _finish(spec, child, arm, "preservation", catalog, cfg)


def reject_unsupported_wrap(spec: QuerySpec) -> Optional[QuerySpec]:
    """Expression wraps around the access key are not given a speculative bound."""
    parsed = _parse_keys(spec.where)
    if parsed is None:
        return None
    column = _bare(parsed[0])
    wrapped = _clone(spec, where=f"ABS({column}) = {sql_literal(parsed[1][0])}")
    wrapped.mutation = "expr_abs_wrap"
    wrapped.bound_outcome = "invalidation"
    return None if wrapped.bound_outcome == "invalidation" else wrapped


_SEED_GENERATORS = (
    ("unique_lookup", gen_unique_lookup),
    ("unique_in", gen_unique_in),
    ("nonunique_lookup", gen_nonunique_lookup),
    ("ordered_range", gen_ordered_range),
    ("ordered_limit", gen_ordered_limit),
    ("boundary_minmax", gen_boundary_minmax),
    ("composite_unique_eq", gen_composite_unique_eq),
    ("desc_ordered_limit", gen_desc_ordered_limit),
    ("expr_abs_lookup", gen_expr_abs_lookup),
    ("partial_pk_lookup", gen_partial_pk_lookup),
    ("covering_lookup", gen_covering_lookup),
    ("partition_eq", gen_partition_eq),
    ("partition_list_eq", gen_partition_list_eq),
    ("partition_hash_eq", gen_partition_hash_eq),
    ("null_contradiction", gen_null_contradiction),
    ("check_domain_baseline", gen_check_domain_baseline),
    ("dual_minmax", gen_dual_minmax),
    ("outer_join_elim", gen_outer_join_elim),
    ("inner_join_elim_fk", gen_inner_join_elim_fk),
    ("index_merge_or", gen_index_merge_or),
    ("unsat_range", gen_unsatisfiable_range),
    ("unsat_eq", gen_unsatisfiable_eq),
)


def seed_query(catalog: Catalog, rng, cfg: Config, cursor: List[int]) -> Optional[QuerySpec]:
    """Anchor one cardinality primitive supported by the current database state.

    The cursor rotates the first generator so later episodes do not all seed
    the same access pattern. A seed is kept only when its bound is selective
    (or the generator already opted out of the tightness check).
    """
    import random

    if not isinstance(rng, random.Random):
        raise TypeError("seed_query expects random.Random")
    gens = [
        (name, gen)
        for name, gen in _SEED_GENERATORS
        if name not in cfg.skip_kinds and (not cfg.only_kinds or name in cfg.only_kinds)
    ]
    if not gens:
        return None
    start = cursor[0] % len(gens)
    cursor[0] += 1
    order = gens[start:] + gens[:start]
    for _name, gen in order:
        spec = gen(catalog, rng, cfg)
        if spec is None:
            continue
        if spec.pivot_table is None:
            bare = spec.from_clause.strip()
            if bare in catalog.tables:
                spec.pivot_table = bare
        parsed = _parse_keys(spec.where)
        if parsed is not None and spec.pivot_column is None:
            spec.pivot_column = _bare(parsed[0])
        spec.bound_outcome = "seed"
        spec.mutation = ""
        if not _selective(spec, catalog, cfg):
            continue
        return _sync_occurrences(spec)
    return None


def propose(
    arm: str,
    pool: Sequence[Parent],
    catalog: Catalog,
    cfg: Config,
    memory: MutationMemory,
    state_key: str,
    commit: bool,
) -> Optional[QuerySpec]:
    """Highest-reward eligible parent, then an untried target in round-robin order."""
    if arm not in QUERY_ARMS or not pool:
        return None
    grouped: Dict[float, List[Parent]] = {}
    for parent in pool:
        grouped.setdefault(parent.reward, []).append(parent)
    for reward in sorted(grouped, reverse=True):
        group = sorted(grouped[reward], key=lambda parent: parent.key)
        start = memory.parent_rr % len(group)
        sequence = group[start:] + group[:start]
        for parent in sequence:
            spec = parent.payload
            if not isinstance(spec, QuerySpec):
                continue
            available = []
            for target in _targets(arm, spec, catalog, cfg):
                mark = (parent.key, arm, target)
                if mark in memory.tried:
                    continue
                available.append(target)
            if not available:
                continue
            cursor = memory.target_rr.get((parent.key, arm), 0) % len(available)
            ordered = available[cursor:] + available[:cursor]
            for offset, target in enumerate(ordered):
                mark = (parent.key, arm, target)
                candidate = _apply(arm, spec, target, catalog, cfg)
                if candidate is None:
                    continue
                fingerprint = ast_fingerprint(candidate, cfg.db)
                cache_key = (state_key, fingerprint)
                if cache_key in memory.cache:
                    if commit:
                        memory.tried.add(mark)
                    continue
                if not commit:
                    return candidate
                memory.tried.add(mark)
                memory.cache.add(cache_key)
                memory.parent_rr += 1
                memory.target_rr[(parent.key, arm)] = cursor + offset + 1
                return candidate
    return None
