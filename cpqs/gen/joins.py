from __future__ import annotations

"""Join generators with derivable unique-key cardinalities.

SEMI JOIN (EXISTS / IN subquery):  |S_left ∩ S_right|
ANTI JOIN (NOT EXISTS / NOT IN):   |S_left − S_right|  (keys are NOT NULL)
INNER / STRAIGHT_JOIN / USING:     1-1 unique equi-join → |key set|
NATURAL JOIN:                      identical generated rows → 1-1 on PK
LEFT/RIGHT JOIN:                   |S_outer| when the preserved side is unique-filtered
CROSS JOIN of two unique IN-lists: |S0| * |S1|
LATERAL / CROSS APPLY:             unique correlated lookup → |S_left|
HASH JOIN is not a SQL join type here: it is an implementation of INNER JOIN.
"""

import random
from typing import List, Optional, Sequence, Tuple

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData, in_list, sql_literal
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import QuerySpec
from cpqs.gen.composite import independent_pair_pred
from cpqs.gen.predicates import (
    pair_values,
    pick_col_int_filter,
    pick_key_set,
    q,
    render_composite_pred,
    render_int_filter,
    render_join_on,
)

MYSQL_FAMILY = {"mysql", "mariadb", "tidb", "oceanbase"}
LATERAL_DBS = {"mysql", "postgres", "cockroach", "opengauss"}  # MariaDB 11.4 / TiDB / ClickHouse / OceanBase: no JOIN LATERAL
APPLY_DBS = {"oracle"}
FULL_OUTER_DBS = {"postgres", "cockroach", "oracle", "opengauss", "clickhouse"}


def inner_join_kw(rng: random.Random, cfg: Config) -> str:
    opts = ["JOIN", "INNER JOIN"]
    if cfg.db in MYSQL_FAMILY:
        opts.append("STRAIGHT_JOIN")
    return rng.choice(opts)


def mixed_join_kw(rng: random.Random, cfg: Config) -> str:
    opts = ["JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN"]
    if cfg.db in MYSQL_FAMILY:
        opts.append("STRAIGHT_JOIN")
    return rng.choice(opts)


def outer_join_kw(rng: random.Random) -> str:
    return rng.choice(["LEFT JOIN", "LEFT OUTER JOIN", "RIGHT JOIN", "RIGHT OUTER JOIN"])


def _uniq(table: TableData, rng: random.Random) -> str:
    cols = table.unique_leading_columns() or [table.pk_column]
    return rng.choice(cols)


def _shared_keys(tables: Sequence[TableData]) -> List[int]:
    keys = set(tables[0].pk_set())
    for t in tables[1:]:
        keys &= set(t.pk_set())
    return sorted(keys)


def _q(table: str, col: str) -> str:
    return q(table, col)


def _pick_tables(catalog: Catalog, rng: random.Random, k: int) -> Optional[List[TableData]]:
    tables = catalog.ordered_tables()
    if len(tables) < k:
        return None
    picked = rng.sample(tables, k)
    if rng.random() < 0.55:
        picked.sort(key=lambda t: int(t.name[1:]))
    return picked


def _choose_njoin(rng: random.Random, n_avail: int, njoin: Optional[int] = None) -> Optional[int]:
    if n_avail < 2:
        return None
    hi = min(4, n_avail)
    if njoin is not None:
        return njoin if 2 <= njoin <= hi else None
    if hi >= 4:
        return rng.choices([2, 3, 4], weights=[10, 40, 50])[0]
    if hi == 3:
        return rng.choices([2, 3], weights=[20, 80])[0]
    return 2


def gen_multitable_inner(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    njoin: Optional[int] = None,
) -> Optional[QuerySpec]:
    n_avail = len(catalog.ordered_tables())
    njoin = _choose_njoin(rng, n_avail, njoin)
    if njoin is None:
        return None
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    shared = _shared_keys(used)
    m = rng.randint(2, 6)
    if len(shared) < m:
        return None
    keys, _contig = pick_key_set(shared, rng, m)
    if not keys:
        return None
    # Prefer composite unique join when both sides have a 2-col unique index.
    comp = _shared_composite(used)
    if comp and rng.random() < 0.4:
        return _composite_inner(used, keys, rng, cfg)
    cols = [_uniq(t, rng) for t in used]
    from_clause = used[0].name
    kws = []
    for i in range(1, njoin):
        kw = mixed_join_kw(rng, cfg)
        kws.append(kw)
        on = render_join_on(used[i - 1].name, cols[i - 1], used[i].name, cols[i], keys, rng)
        from_clause += f" {kw} {used[i].name} ON {on}"
    where = render_int_filter(_q(used[0].name, cols[0]), keys, rng)
    if rng.random() < 0.55:
        where += " AND " + render_int_filter(_q(used[-1].name, cols[-1]), keys, rng)
    return QuerySpec(
        path_kind=f"inner_join_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"{njoin}-table mixed-join ({', '.join(kws)}) on {m} keys",
        notes=["unique 1-1 chain: result cardinality equals the filtered key set"],
    )


def _shared_composite(tables: Sequence[TableData]) -> Optional[Tuple[str, ...]]:
    sets = []
    for t in tables:
        pairs = {idx.keys[:2] for idx in t.composite_unique_indexes() if len(idx.keys) >= 2}
        # drop trailing partition key for comparison of the logical unique pair
        trimmed = set()
        for keys in pairs:
            trimmed.add(keys[:2] if keys[-1] == "part" and len(keys) > 2 else keys[:2])
        sets.append(trimmed)
    common = sets[0]
    for s in sets[1:]:
        common &= s
    if not common:
        return None
    return sorted(common)[0]


def _composite_inner(
    used: List[TableData],
    keys: List[int],
    rng: random.Random,
    cfg: Config,
) -> Optional[QuerySpec]:
    cols = _shared_composite(used)
    if cols is None:
        return None
    pairs = pair_values(used[0], keys, cols)
    if len(pairs) != len(keys):
        return None
    from_clause = used[0].name
    kws = []
    for i in range(1, len(used)):
        kw = mixed_join_kw(rng, cfg)
        kws.append(kw)
        on = " AND ".join(
            f"{_q(used[i - 1].name, c)} = {_q(used[i].name, c)}" for c in cols
        )
        if rng.random() < 0.6:
            on += " AND " + render_composite_pred(used[i].name, cols, pairs, rng, qualify=True)
        from_clause += f" {kw} {used[i].name} ON {on}"
    where = render_composite_pred(used[0].name, cols, pairs, rng, qualify=True)
    njoin = len(used)
    return QuerySpec(
        path_kind=f"composite_join_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(len(keys), frozenset(keys)),
        comment=f"{njoin}-table composite unique join on {cols}",
        notes=["composite unique 1-1 join: |result| = |tuple set|"],
    )


def gen_composite_independent_join(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    njoin: Optional[int] = None,
) -> Optional[QuerySpec]:
    """1-1 composite unique join plus independent per-column predicates."""
    n_avail = len(catalog.ordered_tables())
    njoin = _choose_njoin(rng, n_avail, njoin)
    if njoin is None:
        return None
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    cols = _shared_composite(used)
    if cols is None:
        return None
    got = independent_pair_pred(used[0], cols, rng, cfg, table_alias=used[0].name)
    if got is None:
        return None
    pred, rows, f0, f1 = got
    from_clause = used[0].name
    kws = []
    for i in range(1, njoin):
        kw = mixed_join_kw(rng, cfg)
        kws.append(kw)
        on = " AND ".join(
            f"{_q(used[i - 1].name, c)} = {_q(used[i].name, c)}" for c in cols
        )
        from_clause += f" {kw} {used[i].name} ON {on}"
    return QuerySpec(
        path_kind=f"composite_independent_join_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"{njoin}-table composite unique join ({', '.join(kws)}); {cols[0]}:{f0.kind} AND {cols[1]}:{f1.kind}",
        notes=[
            "composite unique 1-1 join; WHERE uses a different predicate form per key column"
        ],
    )


def gen_straight_join_chain(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    njoin: Optional[int] = None,
) -> Optional[QuerySpec]:
    """STRAIGHT_JOIN pins left-to-right order; cardinality is still the unique key set."""
    if cfg.db not in MYSQL_FAMILY:
        return None
    n_avail = len(catalog.ordered_tables())
    njoin = _choose_njoin(rng, n_avail, njoin)
    if njoin is None:
        return None
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    shared = _shared_keys(used)
    m = rng.randint(2, 6)
    if len(shared) < m:
        return None
    keys, _ = pick_key_set(shared, rng, m)
    if not keys:
        return None
    cols = [_uniq(t, rng) for t in used]
    from_clause = used[0].name
    for i in range(1, njoin):
        on = render_join_on(used[i - 1].name, cols[i - 1], used[i].name, cols[i], keys, rng)
        from_clause += f" STRAIGHT_JOIN {used[i].name} ON {on}"
    where = render_int_filter(_q(used[0].name, cols[0]), keys, rng)
    return QuerySpec(
        path_kind=f"straight_join_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"{njoin}-table STRAIGHT_JOIN unique chain on {m} keys",
        notes=["STRAIGHT_JOIN only pins join order; 1-1 unique equality still yields |keys|"],
    )


def gen_join_using(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    njoin: Optional[int] = None,
) -> Optional[QuerySpec]:
    n_avail = len(catalog.ordered_tables())
    njoin = _choose_njoin(rng, n_avail, njoin)
    if njoin is None:
        return None
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    col = used[0].pk_column
    if any(col not in {c.name for c in t.columns} for t in used):
        return None
    shared = _shared_keys(used)
    m = rng.randint(2, 6)
    if len(shared) < m:
        return None
    keys = rng.sample(shared, m)
    # USING coalesces `col`; qualifying it as t0.col is ERROR 1052 in MySQL.
    # Do not mix STRAIGHT_JOIN/ON into a USING chain.
    from_clause = used[0].name
    kws = []
    for i in range(1, njoin):
        kw = rng.choice(["JOIN", "INNER JOIN"])
        kws.append(kw)
        from_clause += f" {kw} {used[i].name} USING ({col})"
    where = f"{col} IN {in_list(keys)}"
    return QuerySpec(
        path_kind=f"join_using_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"{njoin}-table {' / '.join(kws)} USING ({col})",
    )


def gen_natural_join(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    njoin: Optional[int] = None,
) -> Optional[QuerySpec]:
    """NATURAL JOIN on identically generated tables is 1-1 on the PK."""
    n_avail = len(catalog.ordered_tables())
    njoin = _choose_njoin(rng, n_avail, njoin)
    if njoin is None:
        return None
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    shared = _shared_keys(used)
    m = rng.randint(2, 6)
    if len(shared) < m:
        return None
    keys = rng.sample(shared, m)
    col = used[0].pk_column
    left_kw = rng.choice(["NATURAL JOIN", "NATURAL LEFT JOIN", "NATURAL LEFT OUTER JOIN"])
    from_clause = used[0].name
    for i in range(1, njoin):
        kw = left_kw if i == 1 or rng.random() < 0.7 else "NATURAL JOIN"
        from_clause += f" {kw} {used[i].name}"
    # coalesced NATURAL column; do not qualify with a table alias
    where = f"{col} IN {in_list(keys)}"
    return QuerySpec(
        path_kind=f"natural_join_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"{njoin}-table NATURAL join on identical generated rows",
        notes=["generated tables share row i across all columns, so NATURAL JOIN is 1-1"],
    )


def gen_outer_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """LEFT/RIGHT JOIN: ON unique equality plus a unique IN on the inner side.

    Outer rows with WHERE IN (L) are preserved, so result |L|.
    Unmatched inners become NULL rather than dropping the outer row.
    """
    used = _pick_tables(catalog, rng, 2)
    if used is None:
        return None
    t0, t1 = used
    left_col, right_col = _uniq(t0, rng), _uniq(t1, rng)
    shared = _shared_keys([t0, t1])
    if len(shared) < 6:
        return None
    L = rng.sample(shared, rng.randint(4, 6))
    R = rng.sample(L, rng.randint(1, max(1, len(L) - 2)))
    kw = outer_join_kw(rng)
    if kw.startswith("RIGHT"):
        from_clause = (
            f"{t0.name} {kw} {t1.name} ON {_q(t0.name, left_col)} = {_q(t1.name, right_col)}"
            f" AND {_q(t0.name, left_col)} IN {in_list(R)}"
        )
        where = f"{_q(t1.name, right_col)} IN {in_list(L)}"
        result = frozenset(L)
        kind = "right_join"
    else:
        from_clause = (
            f"{t0.name} {kw} {t1.name} ON {_q(t0.name, left_col)} = {_q(t1.name, right_col)}"
            f" AND {_q(t1.name, right_col)} IN {in_list(R)}"
        )
        where = f"{_q(t0.name, left_col)} IN {in_list(L)}"
        result = frozenset(L)
        kind = "left_join"
    return QuerySpec(
        path_kind=kind,
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(len(result), result),
        comment=f"{kw} preserves outer unique keys {L}; inner restricted to {R}",
        notes=["outer join result cardinality is |outer filter|, not the cartesian product"],
    )


def gen_outer_join_chain(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    njoin: Optional[int] = None,
) -> Optional[QuerySpec]:
    """LEFT JOIN chain of 3–4 tables: the leftmost unique IN is preserved."""
    n_avail = len(catalog.ordered_tables())
    if n_avail < 3:
        return None
    if njoin is None:
        njoin = rng.randint(3, min(4, n_avail))
    elif njoin < 3 or njoin > min(4, n_avail):
        return None
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    shared = _shared_keys(used)
    if len(shared) < 4:
        return None
    L = rng.sample(shared, rng.randint(3, 6))
    cols = [_uniq(t, rng) for t in used]
    from_clause = used[0].name
    for i in range(1, njoin):
        kw = rng.choice(["LEFT JOIN", "LEFT OUTER JOIN"])
        on = f"{_q(used[i - 1].name, cols[i - 1])} = {_q(used[i].name, cols[i])}"
        from_clause += f" {kw} {used[i].name} ON {on}"
    where = f"{_q(used[0].name, cols[0])} IN {in_list(L)}"
    return QuerySpec(
        path_kind=f"left_join_{njoin}t",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(len(L), frozenset(L)),
        comment=f"{njoin}-table LEFT JOIN chain preserves leftmost unique keys",
        notes=["each LEFT JOIN keeps unmatched outer rows, so |result| = |left filter|"],
    )


def gen_semi_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """EXISTS / IN (SELECT): |S_left ∩ S_right| under unique equality."""
    used = _pick_tables(catalog, rng, 2)
    if used is None:
        return None
    t0, t1 = used
    left_col, right_col = _uniq(t0, rng), _uniq(t1, rng)
    shared = _shared_keys([t0, t1])
    if len(shared) < 6:
        return None
    L = rng.sample(shared, rng.randint(4, 6))
    n_hit = rng.randint(1, min(3, len(L)))
    R = rng.sample(L, n_hit)
    extra = [x for x in shared if x not in L]
    if extra and rng.random() < 0.5:
        R = R + rng.sample(extra, min(2, len(extra)))
    inter = frozenset(L) & frozenset(R)
    if not inter:
        return None
    style = rng.choice(["exists", "in_subquery"])
    if style == "exists":
        where = (
            f"{_q(t0.name, left_col)} IN {in_list(L)} AND EXISTS ("
            f"SELECT 1 FROM {t1.name} WHERE {_q(t1.name, right_col)} = {_q(t0.name, left_col)}"
            f" AND {_q(t1.name, right_col)} IN {in_list(R)})"
        )
        tag = "semi_exists"
    else:
        where = (
            f"{_q(t0.name, left_col)} IN {in_list(L)} AND {_q(t0.name, left_col)} IN ("
            f"SELECT {_q(t1.name, right_col)} FROM {t1.name} WHERE {_q(t1.name, right_col)} IN {in_list(R)})"
        )
        tag = "semi_in"
    return QuerySpec(
        path_kind=tag,
        select_list="*",
        from_clause=t0.name,
        where=where,
        oracle=CardinalityOracle.exact_count(len(inter), inter),
        comment=f"unique semi-join {style}: |L ∩ R| = {len(inter)}",
        notes=["SEMI JOIN result is left rows that have a unique match in the right candidate set"],
    )


def gen_semi_join_chain(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Nested EXISTS: |S0 ∩ S1 ∩ S2| on unique equality."""
    used = _pick_tables(catalog, rng, 3)
    if used is None:
        return None
    t0, t1, t2 = used
    cols = [_uniq(t, rng) for t in used]
    shared = _shared_keys(used)
    if len(shared) < 8:
        return None
    core = rng.sample(shared, rng.randint(2, 3))
    def _around(core_keys: List[int]) -> List[int]:
        extra = [x for x in shared if x not in core_keys]
        n_extra = rng.randint(1, min(3, len(extra))) if extra else 0
        return core_keys + (rng.sample(extra, n_extra) if n_extra else [])

    L, R, S = _around(core), _around(core), _around(core)
    inter = frozenset(L) & frozenset(R) & frozenset(S)
    if not inter:
        return None
    where = (
        f"{_q(t0.name, cols[0])} IN {in_list(L)} AND EXISTS ("
        f"SELECT 1 FROM {t1.name} WHERE {_q(t1.name, cols[1])} = {_q(t0.name, cols[0])}"
        f" AND {_q(t1.name, cols[1])} IN {in_list(R)} AND EXISTS ("
        f"SELECT 1 FROM {t2.name} WHERE {_q(t2.name, cols[2])} = {_q(t1.name, cols[1])}"
        f" AND {_q(t2.name, cols[2])} IN {in_list(S)}))"
    )
    return QuerySpec(
        path_kind="semi_exists_3t",
        select_list="*",
        from_clause=t0.name,
        where=where,
        oracle=CardinalityOracle.exact_count(len(inter), inter),
        comment=f"3-table nested EXISTS: |L ∩ R ∩ S| = {len(inter)}",
        notes=["nested unique SEMI JOIN is the intersection of the three candidate sets"],
    )


def gen_anti_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """NOT EXISTS / NOT IN (SELECT): |S_left − S_right|. Generated keys are NOT NULL."""
    used = _pick_tables(catalog, rng, 2)
    if used is None:
        return None
    t0, t1 = used
    left_col, right_col = _uniq(t0, rng), _uniq(t1, rng)
    shared = _shared_keys([t0, t1])
    if len(shared) < 6:
        return None
    L = rng.sample(shared, rng.randint(4, 6))
    R = rng.sample(L, rng.randint(1, max(1, len(L) - 2)))
    remaining = frozenset(L) - frozenset(R)
    if not remaining:
        return None
    style = rng.choice(["not_exists", "not_in"])
    if style == "not_exists":
        where = (
            f"{_q(t0.name, left_col)} IN {in_list(L)} AND NOT EXISTS ("
            f"SELECT 1 FROM {t1.name} WHERE {_q(t1.name, right_col)} = {_q(t0.name, left_col)}"
            f" AND {_q(t1.name, right_col)} IN {in_list(R)})"
        )
        tag = "anti_not_exists"
    else:
        where = (
            f"{_q(t0.name, left_col)} IN {in_list(L)} AND {_q(t0.name, left_col)} NOT IN ("
            f"SELECT {_q(t1.name, right_col)} FROM {t1.name} WHERE {_q(t1.name, right_col)} IN {in_list(R)})"
        )
        tag = "anti_not_in"
    return QuerySpec(
        path_kind=tag,
        select_list="*",
        from_clause=t0.name,
        where=where,
        oracle=CardinalityOracle.exact_count(len(remaining), remaining),
        comment=f"unique anti-join {style}: |L − R| = {len(remaining)}",
        notes=["ANTI JOIN is safe here because unique keys are NOT NULL, so NOT IN ≡ NOT EXISTS"],
    )


def _empty_unique_pred(table: TableData, col: str, rng: random.Random) -> Optional[str]:
    """A unique-column predicate that matches no generated row."""
    existing = [int(v) for v in table.existing_values(col) if v is not None]
    if not existing:
        return None
    lo, hi = min(existing), max(existing)
    cname = _q(table.name, col)
    missing = hi + rng.randint(1, 40)
    miss2 = missing + rng.randint(1, 7)
    choices = [
        f"{cname} = {sql_literal(missing)}",
        f"{cname} IN ({sql_literal(missing)}, {sql_literal(miss2)})",
        f"({cname} = {sql_literal(missing)} OR {cname} = {sql_literal(miss2)})",
        f"{cname} > {sql_literal(hi)}",
        f"{cname} >= {sql_literal(hi + 1)}",
        f"{cname} BETWEEN {sql_literal(hi + 1)} AND {sql_literal(hi + 9)}",
        f"{cname} IS NULL",
        f"{cname} > 0 AND {cname} < 0",
        f"{cname} = {sql_literal(missing)} AND {cname} <> {sql_literal(missing)}",
    ]
    if lo > 0:
        choices.append(f"{cname} < {sql_literal(lo)}")
        choices.append(f"{cname} <= {sql_literal(lo - 1)}")
    other = [c for c in table.unique_leading_columns() if c != col]
    if other:
        o = other[0]
        choices.append(
            f"({cname}, {_q(table.name, o)}) IN (({sql_literal(missing)}, {sql_literal(missing)}))"
        )
    comps = table.composite_unique_indexes()
    if comps:
        a, b = comps[0].keys[0], comps[0].keys[1]
        choices.append(
            f"({_q(table.name, a)}, {_q(table.name, b)}) = ({sql_literal(missing)}, {sql_literal(missing)})"
        )
    return rng.choice(choices)


def _true_on(cfg: Config) -> str:
    return "1 = 1"


def gen_semi_empty_outer(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
) -> Optional[QuerySpec]:
    """Empty unique filter on the preserved/outer side: result and inner probe are [0,0].

    Covers SEMI/ANTI, INNER/STRAIGHT, LEFT, CROSS/comma, LATERAL/APPLY, and 2–4 tables.
    """
    n_avail = len(catalog.ordered_tables())
    njoin = _choose_njoin(rng, n_avail) or 2
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    cols = [_uniq(t, rng) for t in used]
    empty = _empty_unique_pred(used[0], cols[0], rng)
    if empty is None:
        return None
    t0, t1 = used[0], used[1]
    lcol, rcol = cols[0], cols[1]
    shape = rng.choice(
        [
            "exists",
            "in_subquery",
            "anti_exists",
            "anti_not_in",
            "inner",
            "left",
            "cross",
            "comma",
            "lateral",
        ]
    )
    notes = ["empty outer/preserved side is [0,0]; probing inner tables would leak their full cardinality"]
    if shape in {"exists", "in_subquery", "anti_exists", "anti_not_in"} and njoin > 2:
        # nest extra SEMI on t2 when present
        pass
    from_clause = t0.name
    where = empty
    kind = "empty_outer_semi"
    if shape == "exists":
        inner = f"SELECT 1 FROM {t1.name} WHERE {_q(t1.name, rcol)} = {_q(t0.name, lcol)}"
        if njoin >= 3:
            t2, c2 = used[2], cols[2]
            inner += (
                f" AND EXISTS (SELECT 1 FROM {t2.name} WHERE {_q(t2.name, c2)} = {_q(t1.name, rcol)})"
            )
            kind = "empty_outer_semi_3t"
        where = f"{empty} AND EXISTS ({inner})"
    elif shape == "in_subquery":
        inner = f"SELECT {_q(t1.name, rcol)} FROM {t1.name}"
        if njoin >= 3:
            t2, c2 = used[2], cols[2]
            inner += f" WHERE EXISTS (SELECT 1 FROM {t2.name} WHERE {_q(t2.name, c2)} = {_q(t1.name, rcol)})"
            kind = "empty_outer_semi_3t"
        where = f"{empty} AND {_q(t0.name, lcol)} IN ({inner})"
    elif shape == "anti_exists":
        kind = "empty_outer_anti"
        where = (
            f"{empty} AND NOT EXISTS (SELECT 1 FROM {t1.name} "
            f"WHERE {_q(t1.name, rcol)} = {_q(t0.name, lcol)})"
        )
    elif shape == "anti_not_in":
        kind = "empty_outer_anti"
        where = f"{empty} AND {_q(t0.name, lcol)} NOT IN (SELECT {_q(t1.name, rcol)} FROM {t1.name})"
        notes.append("unique keys are NOT NULL, so NOT IN ≡ NOT EXISTS")
    elif shape == "lateral":
        if cfg.db in LATERAL_DBS:
            kind = "empty_outer_lateral"
            from_clause = (
                f"{t0.name} JOIN LATERAL (SELECT * FROM {t1.name} "
                f"WHERE {_q(t1.name, rcol)} = {_q(t0.name, lcol)}) AS lat ON {_true_on(cfg)}"
            )
            where = empty
        elif cfg.db in APPLY_DBS:
            kind = "empty_outer_apply"
            from_clause = (
                f"{t0.name} CROSS APPLY (SELECT * FROM {t1.name} "
                f"WHERE {_q(t1.name, rcol)} = {_q(t0.name, lcol)}) lat"
            )
            where = empty
        else:
            kind = "empty_outer_inner"
            kw = inner_join_kw(rng, cfg)
            from_clause = (
                f"{t0.name} {kw} {t1.name} ON {_q(t0.name, lcol)} = {_q(t1.name, rcol)}"
            )
            where = empty
    elif shape == "comma":
        kind = f"empty_outer_comma_{njoin}t"
        from_clause = ", ".join(t.name for t in used)
        eqs = " AND ".join(
            f"{_q(used[i].name, cols[i])} = {_q(used[i + 1].name, cols[i + 1])}"
            for i in range(njoin - 1)
        )
        where = f"{empty} AND {eqs}" if rng.random() < 0.7 else empty
    elif shape == "cross":
        kind = f"empty_outer_cross_{njoin}t"
        from_clause = f"{t0.name} " + " ".join(f"CROSS JOIN {t.name}" for t in used[1:])
        where = empty
    else:
        kind = f"empty_outer_{'left' if shape == 'left' else 'inner'}_{njoin}t"
        from_clause = t0.name
        for i in range(1, njoin):
            kw = outer_join_kw(rng) if shape == "left" else inner_join_kw(rng, cfg)
            if shape == "left":
                kw = rng.choice(["LEFT JOIN", "LEFT OUTER JOIN"])
            on = f"{_q(used[i - 1].name, cols[i - 1])} = {_q(used[i].name, cols[i])}"
            from_clause += f" {kw} {used[i].name} ON {on}"
        where = empty
    return QuerySpec(
        path_kind=kind,
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"empty unique filter on {t0.name}.{lcol}; join must not scan inner tables",
        notes=notes,
    )


def gen_independent_unique_filters(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
    njoin: Optional[int] = None,
) -> Optional[QuerySpec]:
    """No join equality: each table has its own unique-column filter.

    Result cardinality is the product of per-table candidate sets. A plan that
    cartesian-scans a full table reports that table's N and is a bug.
    """
    n_avail = len(catalog.ordered_tables())
    njoin = _choose_njoin(rng, n_avail, njoin)
    if njoin is None:
        return None
    used = _pick_tables(catalog, rng, njoin)
    if used is None:
        return None
    filters = []
    product = 1
    for t in used:
        col = _uniq(t, rng)
        if njoin >= 3:
            kind = rng.choice(["eq", "eq", "eq", "in", "or_eq"])
        else:
            kind = rng.choice(["eq", "in", "or_eq", "between", "ge_le", "gt_lt", "eq"])
        filt = pick_col_int_filter(t, col, rng, kind, table_alias=t.name)
        if filt is None or not filt.rows:
            return None
        product *= len(filt.rows)
        filters.append(filt)
    if product <= 0:
        return None
    style = rng.choice(["comma", "cross", "inner_true", "left_true"])
    on = _true_on(cfg)
    if style == "comma":
        from_clause = ", ".join(t.name for t in used)
    elif style == "cross":
        from_clause = f"{used[0].name} " + " ".join(f"CROSS JOIN {t.name}" for t in used[1:])
    else:
        kw0 = "LEFT JOIN" if style == "left_true" else None
        from_clause = used[0].name
        for t in used[1:]:
            kw = kw0 if kw0 else inner_join_kw(rng, cfg)
            from_clause += f" {kw} {t.name} ON {on}"
    where = " AND ".join(f.sql for f in filters)
    if rng.random() < 0.35:
        select = ", ".join(_q(t.name, f.col) for t, f in zip(used, filters))
    else:
        select = "*"
    sizes = "×".join(str(len(f.rows)) for f in filters)
    return QuerySpec(
        path_kind=f"indep_unique_{njoin}t",
        select_list=select,
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(product),
        comment=f"independent unique filters, no join key; |S| = {sizes} = {product}",
        notes=["each predicate is on a unique column of one table; the plan must not full-scan any table"],
    )


def gen_const_range_push(
    catalog: Catalog,
    rng: random.Random,
    cfg: Config,
) -> Optional[QuerySpec]:
    """Const unique row supplies the bounds of a composite range on the inner table."""
    used = _pick_tables(catalog, rng, 2)
    if used is None:
        return None
    t0, t1 = used
    if t1.compatible_ordered_index("b", prefix_eq=("a",)) is None:
        return None
    pk_col = t0.pk_column
    pk = rng.choice(list(t0.pk_set()))
    src = t0.row_by_pk(pk)
    if src is None:
        return None
    a, b = src["a"], src["b"]
    width = rng.randint(1, 4)
    hi = b + width
    rows = frozenset(
        p
        for p in t1.rows_for("a", a)
        if (row := t1.row_by_pk(p)) is not None and b <= row["b"] <= hi
    )
    if not rows:
        return None
    kw = inner_join_kw(rng, cfg)
    from_clause = f"{t0.name} {kw} {t1.name} ON {_q(t0.name, 'a')} = {_q(t1.name, 'a')}"
    where = (
        f"{_q(t0.name, pk_col)} = {sql_literal(pk)} AND "
        f"{_q(t1.name, 'b')} >= {_q(t0.name, 'b')} AND "
        f"{_q(t1.name, 'b')} <= {_q(t0.name, 'b')} + {width}"
    )
    return QuerySpec(
        path_kind="const_range_push",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"const {t0.name}.{pk_col}={pk} pushed as {t1.name}.b in [{b},{hi}]",
        notes=["bounds from a const table must be folded into the inner composite range, not left as a filter"],
    )


def gen_lateral_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """LATERAL / CROSS APPLY unique correlated lookup: |S_left|."""
    used = _pick_tables(catalog, rng, 2)
    if used is None:
        return None
    t0, t1 = used
    left_col, right_col = _uniq(t0, rng), _uniq(t1, rng)
    shared = _shared_keys([t0, t1])
    m = rng.randint(2, 6)
    if len(shared) < m:
        return None
    keys = rng.sample(shared, m)
    corr = f"{_q(t1.name, right_col)} = {_q(t0.name, left_col)}"
    if cfg.db in APPLY_DBS:
        from_clause = (
            f"{t0.name} CROSS APPLY ("
            f"SELECT * FROM {t1.name} WHERE {corr}) lat"
        )
        tag = "cross_apply"
    elif cfg.db in LATERAL_DBS:
        from_clause = (
            f"{t0.name} JOIN LATERAL ("
            f"SELECT * FROM {t1.name} WHERE {corr}) AS lat ON TRUE"
        )
        tag = "lateral_join"
    else:
        return None
    where = f"{_q(t0.name, left_col)} IN {in_list(keys)}"
    return QuerySpec(
        path_kind=tag,
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(m, frozenset(keys)),
        comment=f"unique correlated {tag}: each left key finds exactly one right row",
        notes=["LATERAL/APPLY with unique equality is a 1-1 lookup, not a cartesian product"],
    )


def gen_cross_join_bounded(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    used = _pick_tables(catalog, rng, 2)
    if used is None:
        return None
    t0, t1 = used
    left_col, right_col = _uniq(t0, rng), _uniq(t1, rng)
    shared = _shared_keys([t0, t1])
    if len(shared) < 4:
        return None
    L = rng.sample(shared, 2)
    R = rng.sample(shared, 2)
    from_clause = f"{t0.name} CROSS JOIN {t1.name}"
    where = f"{_q(t0.name, left_col)} IN {in_list(L)} AND {_q(t1.name, right_col)} IN {in_list(R)}"
    n = len(L) * len(R)
    return QuerySpec(
        path_kind="cross_join_bounded",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(n),
        comment=f"bounded CROSS JOIN |L|×|R| = {n}; must not scan full tables",
    )


def gen_full_outer_join(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    if cfg.db not in FULL_OUTER_DBS:
        return None
    used = _pick_tables(catalog, rng, 2)
    if used is None:
        return None
    t0, t1 = used
    left_col, right_col = _uniq(t0, rng), _uniq(t1, rng)
    shared = _shared_keys([t0, t1])
    if len(shared) < 6:
        return None
    L = rng.sample(shared, 3)
    R = rng.sample(shared, 3)
    union = frozenset(L) | frozenset(R)
    from_clause = (
        f"{t0.name} FULL OUTER JOIN {t1.name} ON {_q(t0.name, left_col)} = {_q(t1.name, right_col)}"
    )
    where = (
        f"{_q(t0.name, left_col)} IN {in_list(L)} OR {_q(t1.name, right_col)} IN {in_list(R)}"
    )
    return QuerySpec(
        path_kind="full_outer_join",
        select_list="*",
        from_clause=from_clause,
        where=where,
        oracle=CardinalityOracle.exact_count(len(union), union),
        comment=f"FULL OUTER unique join |L ∪ R| = {len(union)}",
    )
