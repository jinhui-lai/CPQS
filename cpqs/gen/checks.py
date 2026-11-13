"""CHECK-constraint cardinality oracles.

Engines that use CHECK constraints to prove emptiness (and thus belong here):

* Postgres / OpenGauss — ``SET constraint_exclusion = on`` (default ``partition``
  ignores CHECKs on ordinary tables). Misses under ``on`` are real bugs.
* Oracle — always considers CHECKs (FILTER ``NULL IS NOT NULL``, child Starts=0);
  no session GUC. Expression wraps and some OR/join shapes still over-access.

MySQL / MariaDB / TiDB / OceanBase / Cockroach enforce CHECK at DML time but do
**not** prove query emptiness from CHECKs (always scan). ClickHouse may prune via
statistics indexes, not CHECK. Those engines are intentionally excluded.

Oracle families
---------------
1. ``check_expr_wrap`` — domain CHECK on non-unique ``k`` / ``payload``, but the
   query wraps the column (``+ 0``, ``abs``, cast, unary minus) so exclusion
   fails and the engine Seq-Scans N rows for an empty result.
2. ``check_pair_arith`` — multi-column CHECK ``k <= c0`` / ``a < b`` defeated by
   arithmetic on one side (``k >= c0 + 1``).
3. ``check_pair_point`` — multi-column CHECK defeated by a contradictory point
   ``a = x AND b = y`` with ``x >= y`` (alone may Index-Scan 0 rows; paired with
   a join OR below to force large work).
4. ``check_or_join_empty`` — ``t.P OR t1.Q`` where each of ``P``, ``Q`` alone is
   CHECK-empty, yet the join runs a full cartesian Nested Loop.
"""

from __future__ import annotations

import random
from typing import List, Optional

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import QuerySpec

# Engines that actually use CHECKs for plan-time emptiness proofs.
CHECK_DBS = frozenset({"postgres", "opengauss", "oracle"})
CHECK_EXCLUSION_GUC_DBS = frozenset({"postgres", "opengauss"})


def _k_domain(table: TableData) -> Optional[int]:
    """Upper bound from ``CHECK (k >= 1 AND k <= D)``, else inferred from data."""
    for chk in table.checks:
        compact = chk.replace(" ", "").lower()
        if compact.startswith("k>=1andk<="):
            try:
                return int(compact.split("<=")[1])
            except ValueError:
                pass
    vals = table.existing_values("k")
    if not vals:
        return None
    return int(max(vals))


def _out_of_k(table: TableData, rng: random.Random) -> Optional[int]:
    dk = _k_domain(table)
    if dk is None:
        return None
    return rng.choice([0, -1, dk + 1, dk + 7, dk + 100])


def _out_of_payload(rng: random.Random) -> int:
    return rng.choice([-1, 97, 200, 1000])


def _wrap_eq(col: str, value: int, rng: random.Random, db: str) -> str:
    """Column expression that defeats plain CHECK matching."""
    options = [f"({col} + 0) = {value}"]
    if db == "oracle":
        options.append(f"CAST({col} AS NUMBER) = {value}")
    else:
        options.append(f"({col})::bigint = {value}")
    if value > 0:
        options.append(f"abs({col}) = {value}")
        options.append(f"(-{col}) = {-value}")
    else:
        options.append(f"(-{col}) > 0")
    return rng.choice(options)


def _exclusion_notes(cfg: Config) -> List[str]:
    if cfg.db in CHECK_EXCLUSION_GUC_DBS:
        return [
            "requires SET constraint_exclusion = on",
            "plain column refs against the same CHECK should fold to Result false",
        ]
    if cfg.db == "oracle":
        return [
            "Oracle uses CHECK for exclusion without a GUC (FILTER NULL IS NOT NULL)",
            "plain column refs against the same CHECK should not start the table scan",
        ]
    return []


def gen_check_expr_wrap(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Domain CHECK + expression wrap ⇒ should be empty, often Seq-Scans N."""
    if cfg.db not in CHECK_DBS:
        return None
    table = catalog.primary()
    if not table.checks:
        return None
    kind = rng.choice(["k", "payload", "k_range"])
    if kind == "k":
        bad = _out_of_k(table, rng)
        if bad is None:
            return None
        pred = _wrap_eq("k", bad, rng, cfg.db)
        note = f"CHECK(k domain) contradicted by wrapped `{pred}`"
    elif kind == "payload":
        bad = _out_of_payload(rng)
        pred = _wrap_eq("payload", bad, rng, cfg.db)
        note = f"CHECK(payload domain) contradicted by wrapped `{pred}`"
    else:
        dk = _k_domain(table)
        if dk is None:
            return None
        lo, hi = dk + 1, dk + 50
        range_opts = [
            f"(k + 0) > {dk}",
            f"(k + 0) BETWEEN {lo} AND {hi}",
            f"(k + 0) >= {lo} AND (k + 0) <= {hi}",
            f"abs(k) > {dk}",
        ]
        pred = rng.choice(range_opts)
        note = f"CHECK(k domain) contradicted by wrapped range `{pred}`"
    return QuerySpec(
        path_kind="check_expr_wrap",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=note,
        notes=_exclusion_notes(cfg),
    )


def gen_check_pair_arith(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Multi-column CHECK defeated by arithmetic on one side."""
    if cfg.db not in CHECK_DBS:
        return None
    table = catalog.primary()
    if not table.checks:
        return None
    pk = table.pk_column
    pred = rng.choice(
        [
            f"k >= {pk} + 1",
            f"k > {pk} + 0",
            f"(k + 0) > {pk}",
            f"a >= b + 0",
            f"(a + 0) >= b",
            f"a > b - 0",
        ]
    )
    return QuerySpec(
        path_kind="check_pair_arith",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"multi-column CHECK contradicted by arithmetic `{pred}`",
        notes=["plain `k > c0` / `a >= b` fold; `c0 + 1` / `+ 0` wraps often miss"],
    )


def gen_check_pair_point(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Point that violates ``CHECK (a < b)`` or ``CHECK (k <= pk)``.

    Alone this often Index-Scans ~1 key and stays under the dual-validation
    threshold; still emitted so join/OR compositions and larger N can catch it.
    """
    if cfg.db not in CHECK_DBS:
        return None
    table = catalog.primary()
    if not table.checks:
        return None
    pk = table.pk_column
    if rng.random() < 0.5:
        # a < b is invariant; pick a >= b.
        a_vals = table.existing_values("a")
        if not a_vals:
            return None
        a = int(rng.choice(a_vals))
        b = a - rng.randint(0, 3)
        pred = f"a = {a} AND b = {b}"
        note = f"CHECK(a < b) contradicted by point ({a},{b})"
    else:
        dk = _k_domain(table) or 100
        k = rng.randint(2, dk)
        c0 = rng.randint(1, k - 1)
        pred = f"k = {k} AND {pk} = {c0}"
        note = f"CHECK(k <= {pk}) contradicted by point k={k}, {pk}={c0}"
    return QuerySpec(
        path_kind="check_pair_point",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=note,
        notes=["plain inequality vs CHECK folds; conjunctive point often still scans"],
    )


def _empty_side(table: TableData, alias: str, rng: random.Random) -> Optional[str]:
    """One CHECK-empty predicate on ``alias``.

    Prefer plain domain contradictions that fold alone under exclusion=on; the
    bug is that OR-across-join fails to compose those proofs.
    """
    dk = _k_domain(table)
    choices: List[str] = []
    if dk is not None:
        bad = dk + rng.randint(1, 9)
        choices.extend(
            [
                f"{alias}.k = {bad}",
                f"{alias}.k > {dk}",
                f"{alias}.k IN ({bad}, {bad + 1})",
            ]
        )
    choices.append(f"{alias}.payload < 0")
    choices.append(f"{alias}.payload = {_out_of_payload(rng)}")
    choices.append(f"{alias}.a >= {alias}.b")
    choices.append(f"{alias}.k > {alias}.{table.pk_column}")
    a_vals = table.existing_values("a")
    if a_vals:
        a = int(rng.choice(a_vals))
        choices.append(f"{alias}.a = {a} AND {alias}.b = {a - 1}")
    if not choices:
        return None
    return rng.choice(choices)


def gen_check_or_join_empty(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """OR of two independently CHECK-empty sides across a comma join.

    Each side alone is empty (often folded); together the planner builds a
    Nested Loop with a join-filter OR and examines N×M rows.
    """
    if cfg.db not in CHECK_DBS:
        return None
    tables = catalog.ordered_tables()
    if len(tables) < 2:
        return None
    t0, t1 = tables[0], tables[1]
    if not t0.checks or not t1.checks:
        return None
    left = _empty_side(t0, t0.name, rng)
    right = _empty_side(t1, t1.name, rng)
    if left is None or right is None:
        return None
    pred = f"({left}) OR ({right})"
    return QuerySpec(
        path_kind="check_or_join_empty",
        select_list="*",
        from_clause=f"{t0.name}, {t1.name}",
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"OR of two CHECK-empty predicates across join: {pred}",
        notes=[
            "each disjunct alone is empty under constraint_exclusion=on",
            "cartesian Nested Loop with join-filter OR is the miss",
        ],
    )


def gen_check_domain_baseline(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Plain domain contradiction — should fold with exclusion=on (negative control).

    Emitted so a regression that stops folding is still caught; under a correct
    planner access_rows stays 0 and dual-validation does not fire.
    """
    if cfg.db not in CHECK_DBS:
        return None
    table = catalog.primary()
    if not table.checks:
        return None
    dk = _k_domain(table)
    if dk is None:
        return None
    bad = dk + rng.randint(1, 5)
    pred = rng.choice(
        [
            f"k = {bad}",
            f"k > {dk}",
            f"k IN ({bad}, {bad + 1})",
            f"payload = {_out_of_payload(rng)}",
            f"payload < 0",
            f"a >= b",
            f"k > {table.pk_column}",
        ]
    )
    notes = _exclusion_notes(cfg)
    if cfg.db == "postgres":
        notes = notes + [
            "negative control on Postgres: Result + One-Time Filter false under exclusion=on",
        ]
    elif cfg.db == "opengauss":
        notes = notes + [
            "OpenGauss often fails to fold multi-column CHECK inequalities (a>=b, k>c0)",
        ]
    elif cfg.db == "oracle":
        notes = notes + [
            "Oracle folds plain domain eqs; multi-column inequalities may still index-probe",
        ]
    return QuerySpec(
        path_kind="check_domain_baseline",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"CHECK contradiction baseline `{pred}`",
        notes=notes,
    )
