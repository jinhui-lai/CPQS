from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, FrozenSet, List, Optional, Sequence, Tuple

from cpqs.core.catalog import TableData, in_list, sql_literal

# Independent per-column forms used on composite unique pairs.
COL_PRED_KINDS: Tuple[str, ...] = (
    "eq",
    "in",
    "or_eq",
    "between",
    "ge_le",
    "gt_lt",
    "gt_le",
    "ge_lt",
)

# Preferred pairs: the two columns use *different* predicate styles.
INDEPENDENT_KIND_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("in", "between"),
    ("between", "in"),
    ("gt_lt", "eq"),
    ("eq", "in"),
    ("in", "eq"),
    ("ge_le", "or_eq"),
    ("or_eq", "between"),
    ("between", "eq"),
    ("eq", "between"),
    ("gt_le", "in"),
    ("in", "gt_lt"),
    ("ge_lt", "eq"),
    ("or_eq", "ge_le"),
    ("between", "or_eq"),
    ("gt_lt", "in"),
    ("in", "ge_lt"),
    ("ge_le", "in"),
    ("in", "ge_le"),
    ("or_eq", "eq"),
    ("eq", "or_eq"),
    ("gt_lt", "or_eq"),
    ("or_eq", "gt_lt"),
    ("ge_lt", "in"),
    ("between", "gt_lt"),
    ("gt_le", "eq"),
    ("eq", "gt_le"),
)


@dataclass(frozen=True)
class ColFilter:
    """One integer-column predicate plus the generated rows that match it."""

    sql: str
    rows: FrozenSet[int]
    kind: str
    col: str
    values: FrozenSet[Any]


def is_contiguous(keys: Sequence[int]) -> bool:
    if len(keys) < 2:
        return False
    s = sorted(keys)
    return all(s[i] + 1 == s[i + 1] for i in range(len(s) - 1))


def pick_key_set(shared: Sequence[int], rng: random.Random, m: int) -> Tuple[List[int], bool]:
    """Return m keys; contiguous=True when they form an integer interval."""
    if len(shared) < m:
        return [], False
    if rng.random() < 0.5:
        start = rng.randint(0, len(shared) - m)
        keys = list(shared[start : start + m])
        return keys, is_contiguous(keys)
    return rng.sample(list(shared), m), False


def q(table: str, col: str) -> str:
    return f"{table}.{col}"


def pick_col_int_filter(
    table: TableData,
    col: str,
    rng: random.Random,
    kind: str,
    table_alias: Optional[str] = None,
) -> Optional[ColFilter]:
    """Predicate on one integer column; matching PKs are counted from generated rows.

    Discrete forms (``eq`` / ``IN`` / ``OR`` of ``=``) bind a sampled value set.
    Range forms bind a contiguous window of existing values, so BETWEEN/inequalities
    do not pull in extra keys beyond that window.
    """
    ordered = [v for v in table.existing_values(col) if v is not None]
    if not ordered:
        return None
    cname = q(table_alias, col) if table_alias else col

    if kind in {"eq", "in", "or_eq"}:
        need = 1 if kind == "eq" else rng.randint(2, min(6, len(ordered)))
        if len(ordered) < need:
            return None
        picked = rng.sample(ordered, need)
        values = frozenset(picked)
        rows = frozenset().union(*(table.rows_for(col, v) for v in picked))
        if kind == "eq":
            sql = f"{cname} = {sql_literal(picked[0])}"
        elif kind == "or_eq":
            sql = "(" + " OR ".join(f"{cname} = {sql_literal(v)}" for v in picked) + ")"
        else:
            sql = f"{cname} IN {in_list(picked)}"
        return ColFilter(sql, rows, kind, col, values)

    if len(ordered) < 2:
        return None
    min_span = 3 if kind == "gt_lt" else 2
    if len(ordered) < min_span:
        return None
    span = rng.randint(min_span, min(8, len(ordered)))
    start = rng.randint(0, len(ordered) - span)
    window = ordered[start : start + span]
    low, high = window[0], window[-1]
    if kind == "between":
        matched = window
        sql = f"{cname} BETWEEN {sql_literal(low)} AND {sql_literal(high)}"
    elif kind == "ge_le":
        matched = window
        sql = f"{cname} >= {sql_literal(low)} AND {cname} <= {sql_literal(high)}"
    elif kind == "gt_lt":
        matched = window[1:-1]
        if not matched:
            return None
        sql = f"{cname} > {sql_literal(low)} AND {cname} < {sql_literal(high)}"
    elif kind == "gt_le":
        matched = window[1:]
        if not matched:
            return None
        sql = f"{cname} > {sql_literal(low)} AND {cname} <= {sql_literal(high)}"
    elif kind == "ge_lt":
        matched = window[:-1]
        if not matched:
            return None
        sql = f"{cname} >= {sql_literal(low)} AND {cname} < {sql_literal(high)}"
    else:
        return None
    values = frozenset(matched)
    rows = frozenset().union(*(table.rows_for(col, v) for v in matched))
    return ColFilter(sql, rows, kind, col, values)


def render_int_filter(col: str, keys: Sequence[int], rng: random.Random) -> str:
    """Unique-key set as IN / OR / BETWEEN / range / combinations."""
    keys = list(keys)
    if not keys:
        return "1 = 1"
    if len(keys) == 1:
        return f"{col} = {sql_literal(keys[0])}"
    styles = ["in", "or_eq"]
    if is_contiguous(keys):
        styles.extend(["between", "ge_le", "gt_lt", "in_and_between"])
    style = rng.choice(styles)
    lo, hi = min(keys), max(keys)
    if style == "or_eq":
        return "(" + " OR ".join(f"{col} = {sql_literal(k)}" for k in keys) + ")"
    if style == "between":
        return f"{col} BETWEEN {sql_literal(lo)} AND {sql_literal(hi)}"
    if style == "ge_le":
        return f"{col} >= {sql_literal(lo)} AND {col} <= {sql_literal(hi)}"
    if style == "gt_lt":
        return f"{col} > {sql_literal(lo - 1)} AND {col} < {sql_literal(hi + 1)}"
    if style == "in_and_between":
        return f"{col} IN {in_list(keys)} AND {col} BETWEEN {sql_literal(lo)} AND {sql_literal(hi)}"
    return f"{col} IN {in_list(keys)}"


def render_join_on(
    left: str,
    lcol: str,
    right: str,
    rcol: str,
    keys: Sequence[int],
    rng: random.Random,
) -> str:
    """Equi-join plus IN / BETWEEN / OR / tuple IN on the same unique keys."""
    eq = f"{q(left, lcol)} = {q(right, rcol)}"
    lpred = render_int_filter(q(left, lcol), keys, rng)
    rpred = render_int_filter(q(right, rcol), keys, rng)
    tuples = ", ".join(f"({k}, {k})" for k in keys)
    or_pairs = " OR ".join(
        f"({q(left, lcol)} = {sql_literal(k)} AND {q(right, rcol)} = {sql_literal(k)})" for k in keys
    )
    #  we set p=0.0001 to keep 1/10000 of the styles, to avoid many similar bug reports
    tuple_in = f"({q(left, lcol)}, {q(right, rcol)}) IN ({tuples})"
    if rng.random() < 0.0001:
        return tuple_in
    styles = [
        eq,
        f"{eq} AND {lpred}",
        f"{eq} AND {lpred} AND {rpred}",
        f"({or_pairs})",
    ]
    return rng.choice(styles)


def render_composite_pred(
    table: str,
    cols: Sequence[str],
    pairs: Sequence[Sequence[object]],
    rng: random.Random,
    qualify: bool = True,
) -> str:
    """Predicate on a unique column group: tuple IN, AND of equalities, prefix+IN."""
    def _c(col: str) -> str:
        return q(table, col) if qualify else col

    if not cols or not pairs:
        return "1 = 1"
    if len(cols) == 1:
        return render_int_filter(_c(cols[0]), [int(p[0]) for p in pairs], rng)
    tuples = ", ".join("(" + ", ".join(sql_literal(v) for v in p) + ")" for p in pairs)
    eq_and = " OR ".join(
        "(" + " AND ".join(f"{_c(c)} = {sql_literal(v)}" for c, v in zip(cols, p)) + ")"
        for p in pairs
    )
    styles = [f"({', '.join(_c(c) for c in cols)}) IN ({tuples})", f"({eq_and})"]
    if len(pairs) == 1:
        styles.append(" AND ".join(f"{_c(c)} = {sql_literal(v)}" for c, v in zip(cols, pairs[0])))
    prefix = cols[0]
    prefix_vals = sorted({p[0] for p in pairs})
    if len(cols) == 2 and len(prefix_vals) == 1:
        rest = [p[1] for p in pairs]
        styles.append(
            f"{_c(prefix)} = {sql_literal(prefix_vals[0])} AND {render_int_filter(_c(cols[1]), rest, rng)}"
        )
    return rng.choice(styles)


def pair_values(table: TableData, pks: Sequence[int], cols: Sequence[str]) -> List[Tuple[object, ...]]:
    out: List[Tuple[object, ...]] = []
    for pk in pks:
        pair = table.pair_for_pk(int(pk), cols)
        if pair is not None:
            out.append(pair)
    return out
