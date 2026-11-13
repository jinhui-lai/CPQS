"""RANGE partition cardinality oracles (single-table; not empty-outer joins).

Schema: column ``part`` ∈ {1..n_parts}, RANGE bounds ``[p, p+1)`` (Postgres
``FROM (p) TO (p+1)`` / MySQL-family ``VALUES LESS THAN (p+1)``), optional
``pmax`` catch-all on MySQL/TiDB/Oracle/OpenGauss.

Oracle families
---------------
1. ``partition_eq`` / ``partition_range`` / ``partition_and_unique`` — prune to
   the matching partition(s); access must stay near that row count (not N).
2. ``partition_out_of_range`` — predicate outside every populated bound should
   prove empty (``Result`` / const-false / zero-partition). Plain eqs often
   fold; used as baseline + to catch engines that still probe.
3. ``partition_bound_hole`` — exclusive upper-bound incompleteness, e.g.
   ``part > n_parts`` while the last bound is ``TO (n_parts+1)`` / ``LESS THAN
   (n_parts+1)``. Empty, yet still scans the last (and sometimes pmax) partition.
4. ``partition_expr_prune`` — ``(part+0)=p`` / cast / abs should still prune to
   one partition; wrapping often forces Append / RANGE ALL / partition:all.
5. ``partition_expr_empty`` — wrapped out-of-range (``(part+0)=n_parts+9``)
   should be empty without scanning every partition.
"""

from __future__ import annotations

import random
from typing import Optional

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData, in_list, sql_literal
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import QuerySpec

# Dialects that emit RANGE partitions in schema.py (cockroach skipped).
PARTITION_DBS = frozenset(
    {"mysql", "mariadb", "tidb", "postgres", "opengauss", "oracle", "oceanbase"}
)


def _partitioned_table(catalog: Catalog, rng: random.Random) -> Optional[TableData]:
    tables = [t for t in catalog.ordered_tables() if t.partitioned and t.partition is not None]
    if not tables:
        return None
    return rng.choice(tables)


def _part_pred(col: str, parts: list[int], rng: random.Random) -> str:
    if len(parts) == 1:
        p = parts[0]
        return rng.choice(
            [
                f"{col} = {sql_literal(p)}",
                f"{col} IN {in_list([p])}",
                f"{col} >= {sql_literal(p)} AND {col} < {sql_literal(p + 1)}",
                f"{col} BETWEEN {sql_literal(p)} AND {sql_literal(p)}",
            ]
        )
    lo, hi = min(parts), max(parts)
    return rng.choice(
        [
            f"{col} IN {in_list(parts)}",
            f"{col} >= {sql_literal(lo)} AND {col} <= {sql_literal(hi)}",
            f"{col} BETWEEN {sql_literal(lo)} AND {sql_literal(hi)}",
            f"{col} > {sql_literal(lo - 1)} AND {col} < {sql_literal(hi + 1)}",
        ]
    )


def _wrap_part_eq(col: str, value: int, rng: random.Random, db: str) -> str:
    options = [
        f"({col} + 0) = {value}",
        f"abs({col}) = {value}" if value > 0 else f"({col} + 0) = {value}",
    ]
    if db == "oracle":
        options.append(f"CAST({col} AS NUMBER) = {value}")
    elif db in {"postgres", "opengauss"}:
        options.append(f"({col})::bigint = {value}")
    else:
        options.append(f"CAST({col} AS SIGNED) = {value}")
    return rng.choice(options)


def gen_partition_eq(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Predicate on RANGE partition key: only that partition should be scanned."""
    if cfg.db not in PARTITION_DBS:
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None or table.partition.kind != "range":
        return None
    col = table.partition.column
    p = rng.randint(1, table.partition.n_parts)
    rows = table.rows_in_partition(p)
    if not rows:
        return None
    pred = _part_pred(col, [p], rng)
    return QuerySpec(
        path_kind="partition_eq",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"RANGE partition prune {col}={p}; |partition|={len(rows)}",
        notes=["partition predicate should not scan other RANGE partitions"],
        skip_tightness=True,
    )


def gen_partition_range(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Adjacent RANGE partitions: scan only those partitions."""
    if cfg.db not in PARTITION_DBS:
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None or table.partition.kind != "range":
        return None
    spec = table.partition
    if spec.n_parts < 2:
        return None
    width = rng.randint(1, min(2, spec.n_parts))
    start = rng.randint(1, spec.n_parts - width + 1)
    parts = list(range(start, start + width))
    rows = frozenset().union(*[table.rows_in_partition(p) for p in parts])
    if not rows:
        return None
    pred = _part_pred(spec.column, parts, rng)
    return QuerySpec(
        path_kind="partition_range",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"RANGE prune {spec.column} in {parts}; |rows|={len(rows)}",
        notes=["RANGE predicate should skip partitions outside the bound"],
        skip_tightness=True,
    )


def gen_partition_and_unique(catalog: Catalog, rng: random.Random, cfg: Config) -> Optional[QuerySpec]:
    """Partition prune AND unique IN of keys inside that partition."""
    if cfg.db not in PARTITION_DBS:
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None or table.partition.kind != "range":
        return None
    col = table.partition.column
    p = rng.randint(1, table.partition.n_parts)
    part_rows = sorted(table.rows_in_partition(p))
    if len(part_rows) < 2:
        return None
    m = rng.randint(1, min(6, len(part_rows)))
    keys = rng.sample(part_rows, m)
    pred = f"{_part_pred(col, [p], rng)} AND {table.pk_column} IN {in_list(keys)}"
    return QuerySpec(
        path_kind="partition_and_unique",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(keys), frozenset(keys)),
        comment=f"partition {p} plus unique IN of {m} keys",
        notes=["partition prune plus unique lookup; must not scan the full table"],
    )


def gen_partition_out_of_range(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Plain predicate outside all RANGE bounds ⇒ empty (Postgres only).

    MySQL-family / Oracle / OpenGauss emit a ``pmax`` catch-all, so
    ``part = 0`` lands in ``p1`` (``< 2``) and ``part = 99`` lands in ``pmax`` —
    scanning those partitions is correct pruning, not a fold miss. Only
    Postgres (no pmax) can prove emptiness via partition bounds alone.
    """
    # No pmax ⇒ out-of-range keys match zero partitions.
    if cfg.db != "postgres":
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None or table.partition.kind != "range":
        return None
    col = table.partition.column
    n = table.partition.n_parts
    bad = n + rng.randint(1, 9)
    pred = rng.choice(
        [
            f"{col} = {sql_literal(0)}",
            f"{col} = {sql_literal(-1)}",
            f"{col} = {sql_literal(bad)}",
            f"{col} IN {in_list([0, bad])}",
            f"{col} < {sql_literal(1)}",
            f"{col} >= {sql_literal(n + 1)}",
        ]
    )
    return QuerySpec(
        path_kind="partition_out_of_range",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"out-of-range partition key `{pred}` should prove empty",
        notes=[
            "single-table partition emptiness (not empty-outer join)",
            "Postgres: no pmax, so these keys match zero partitions",
        ],
    )


def gen_partition_bound_hole(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Exclusive upper-bound hole: ``part > n_parts`` is empty but may still scan.

    Only meaningful without ``pmax``: last partition is ``[n_parts, n_parts+1)``,
    so ``part > n_parts`` matches no integer yet often still opens the last
    child. With ``pmax``, ``part > n_parts`` correctly routes to the catch-all.
    """
    if cfg.db != "postgres":
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None or table.partition.kind != "range":
        return None
    col = table.partition.column
    n = table.partition.n_parts
    pred = rng.choice(
        [
            f"{col} > {sql_literal(n)}",
            f"{col} > {sql_literal(n)} AND {col} < {sql_literal(n + 100)}",
            f"({col} > {sql_literal(n)})",
        ]
    )
    return QuerySpec(
        path_kind="partition_bound_hole",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"RANGE exclusive-bound hole `{pred}` (empty, must not scan last part)",
        notes=[
            f"last bound is {col} < {n + 1}; {col} > {n} is empty for integers",
            f"contrast: {col} >= {n + 1} often folds to Result / zero parts",
            "single-table only; Postgres without pmax",
        ],
    )


def gen_partition_expr_prune(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Expression wrap on partition key should still prune to one partition."""
    if cfg.db not in PARTITION_DBS:
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None or table.partition.kind != "range":
        return None
    col = table.partition.column
    p = rng.randint(1, table.partition.n_parts)
    rows = table.rows_in_partition(p)
    if not rows:
        return None
    pred = _wrap_part_eq(col, p, rng, cfg.db)
    return QuerySpec(
        path_kind="partition_expr_prune",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"wrapped partition key `{pred}` must still prune to part={p}",
        notes=[
            f"plain `{col} = {p}` prunes; wraps often scan all partitions",
            "single-table prune miss (Append / partition:all / RANGE ALL)",
        ],
        skip_tightness=True,
    )


def gen_partition_expr_empty(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Wrapped out-of-range partition key ⇒ empty, must not scan every part."""
    if cfg.db not in PARTITION_DBS:
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None:
        return None
    # Expression-empty proofs are RANGE-specific (LIST/HASH catch-alls differ).
    if table.partition.kind != "range":
        return None
    col = table.partition.column
    n = table.partition.n_parts
    bad = n + rng.randint(5, 20)
    pred = rng.choice(
        [
            _wrap_part_eq(col, bad, rng, cfg.db),
            f"({col} + 0) > {sql_literal(n)}",
            f"({col} + 0) IN {in_list([0, bad])}",
            f"abs({col}) = {bad}",
        ]
    )
    return QuerySpec(
        path_kind="partition_expr_empty",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(0, frozenset()),
        comment=f"wrapped out-of-range `{pred}` should be empty without full Append",
        notes=[
            "plain out-of-range often folds; wraps defeat partition pruning",
            "single-table only (not empty-outer join)",
        ],
    )


def _rows_for_partition(table: TableData, part_id: int) -> frozenset:
    spec = table.partition
    if spec is None:
        return frozenset()
    if spec.kind == "hash":
        out = []
        for row in table.rows:
            if table.hash_partition_id(int(row[spec.column])) == part_id:
                out.append(int(row[table.pk_column]))
        return frozenset(out)
    return table.rows_in_partition(part_id)


def gen_partition_list_eq(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """LIST partition equality: only the matching list partition is scanned."""
    if cfg.db not in PARTITION_DBS:
        return None
    tables = [
        t
        for t in catalog.ordered_tables()
        if t.partitioned and t.partition is not None and t.partition.kind == "list"
    ]
    if not tables:
        return None
    table = rng.choice(tables)
    spec = table.partition
    assert spec is not None
    p = rng.randint(1, spec.n_parts)
    rows = _rows_for_partition(table, p)
    if not rows:
        return None
    pred = _part_pred(spec.column, [p], rng)
    return QuerySpec(
        path_kind="partition_list_eq",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"LIST partition prune {spec.column}={p}; |partition|={len(rows)}",
        notes=["LIST predicate should not scan other list partitions"],
        skip_tightness=True,
    )


def gen_partition_hash_eq(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """HASH partition equality: only the bucket for ``part = p`` is scanned.

    ``P'`` is computed with the generator-side ``value % n_parts`` map, matching
    the HASH DDL emitted for MySQL-family / Oracle.
    """
    if cfg.db not in PARTITION_DBS:
        return None
    tables = [
        t
        for t in catalog.ordered_tables()
        if t.partitioned and t.partition is not None and t.partition.kind == "hash"
    ]
    if not tables:
        return None
    table = rng.choice(tables)
    spec = table.partition
    assert spec is not None
    # Equality on a concrete part value collapses to one HASH bucket.
    p = rng.randint(1, spec.n_parts)
    rows = table.rows_in_partition(p)
    if not rows:
        return None
    pred = f"{spec.column} = {sql_literal(p)}"
    return QuerySpec(
        path_kind="partition_hash_eq",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(rows), rows),
        comment=f"HASH partition prune {spec.column}={p}; |bucket|={len(rows)}",
        notes=["equality on the hash key selects one bucket; full HASH ALL is a miss"],
        skip_tightness=True,
    )


def gen_partition_local_index(
    catalog: Catalog, rng: random.Random, cfg: Config
) -> Optional[QuerySpec]:
    """Partition prune plus unique lookup inside the surviving partition (P0 #13)."""
    if cfg.db not in PARTITION_DBS:
        return None
    table = _partitioned_table(catalog, rng)
    if table is None or table.partition is None:
        return None
    spec = table.partition
    p = rng.randint(1, spec.n_parts)
    part_rows = sorted(_rows_for_partition(table, p) if spec.kind == "hash" else table.rows_in_partition(p))
    if len(part_rows) < 2:
        return None
    m = rng.randint(1, min(5, len(part_rows)))
    keys = rng.sample(part_rows, m)
    # For HASH, equality on the concrete part value is the prune key.
    part_pred = (
        f"{spec.column} = {sql_literal(p)}"
        if spec.kind in {"list", "hash"}
        else _part_pred(spec.column, [p], rng)
    )
    pred = f"{part_pred} AND {table.pk_column} IN {in_list(keys)}"
    return QuerySpec(
        path_kind="partition_local_index",
        select_list="*",
        from_clause=table.name,
        where=pred,
        oracle=CardinalityOracle.exact_count(len(keys), frozenset(keys)),
        comment=f"{spec.kind} partition {p} plus local unique IN of {m} keys",
        notes=["combined prune + index; must not scan the full table or other partitions"],
    )
