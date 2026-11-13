from __future__ import annotations

from typing import List

from cpqs.core.catalog import TableData

U_PAIR = 10


def k_of(i: int, distinct_k: int, k_gap: int = 1) -> int:
    """Indexed ``k`` for row ``i``. ``k_gap`` spreads values so a fixed numeric
    window matches fewer keys (range selectivity). ``k_gap=1`` is the dense
    ``1..distinct_k`` sequence.
    """
    gap = max(1, k_gap)
    return ((i - 1) % distinct_k) * gap + 1


def payload_of(i: int, null_stride: int = 0):
    """``payload`` value. A positive stride writes NULL on every stride-th row."""
    if null_stride and i % null_stride == 0:
        return None
    return (i * 17) % 97


def assign_part(i: int, n: int, n_parts: int, width: int, major_pct: int = 0) -> int:
    """Partition key for row ``i``.

    ``major_pct=0`` stripes rows evenly. A positive percentage places that
    share of the table in partition 1 and stripes the remainder across the
    other partitions, which changes partition occupancy without changing
    the partition bounds.
    """
    if major_pct <= 0 or n_parts < 2:
        return min(n_parts, ((i - 1) // width) + 1)
    cutoff = max(1, (n * major_pct) // 100)
    if i <= cutoff:
        return 1
    rest = max(1, n - cutoff)
    rest_parts = n_parts - 1
    width_rest = max(1, (rest + rest_parts - 1) // rest_parts)
    return min(n_parts, 2 + (i - cutoff - 1) // width_rest)


def insert_rows_sql(table: TableData, batch_size: int = 500) -> List[str]:
    """Emit batched INSERTs. Used as a fallback; adapters prefer set-based loads."""
    cols = [c.name for c in table.base_columns()]
    col_list = ", ".join(cols)
    stmts: List[str] = []
    batch: List[str] = []
    for row in table.rows:
        values = ", ".join("NULL" if row[c] is None else str(int(row[c])) for c in cols)
        batch.append(f"({values})")
        if len(batch) >= batch_size:
            stmts.append(f"INSERT INTO {table.name} ({col_list}) VALUES {', '.join(batch)}")
            batch = []
    if batch:
        stmts.append(f"INSERT INTO {table.name} ({col_list}) VALUES {', '.join(batch)}")
    return stmts


def set_based_insert_sql(table: TableData, dialect: str, distinct_k: int) -> str:
    """Compact INSERT..SELECT so POCs stay readable at 10k+ rows."""
    n = table.n
    group = max(1, n // distinct_k)
    width = table.partition.width if table.partition else max(1, (n + 9) // 10)
    n_parts = table.partition.n_parts if table.partition else 3
    select = _number_select(dialect, n, distinct_k, group, width, n_parts)
    # Generated columns are computed by the engine; do not list them in INSERT.
    cols = ", ".join(c.name for c in table.base_columns())
    select = _apply_distribution(select, dialect, table, distinct_k, n, width, n_parts)
    return f"INSERT INTO {table.name} ({cols}) {select}"


def _apply_distribution(
    select: str,
    dialect: str,
    table: TableData,
    distinct_k: int,
    n: int,
    width: int,
    n_parts: int,
) -> str:
    """Rewrite k / part / payload in an otherwise default INSERT..SELECT.

    Default knobs leave the statement unchanged. The replacements match
    :func:`k_of`, :func:`assign_part`, and :func:`payload_of`.
    """
    gap = max(1, getattr(table, "k_gap", 1))
    stride = max(0, getattr(table, "null_stride", 0))
    major = max(0, getattr(table, "part_major_pct", 0))
    if gap == 1 and stride == 0 and major == 0:
        return select
    if gap != 1:
        select = select.replace(_k_sql(dialect, distinct_k, 1), _k_sql(dialect, distinct_k, gap), 1)
    if major > 0 and n_parts >= 2:
        select = select.replace(
            _part_sql(dialect, n_parts, width, 0, n),
            _part_sql(dialect, n_parts, width, major, n),
            1,
        )
    if stride > 0:
        select = select.replace(_payload_sql(dialect, 0), _payload_sql(dialect, stride), 1)
    return select


def _k_sql(dialect: str, distinct_k: int, k_gap: int) -> str:
    if dialect == "cockroach":
        base = f"mod(i - 1, {distinct_k})"
    elif dialect == "clickhouse":
        base = f"(i - 1) % {distinct_k}"
    else:
        base = f"MOD(i - 1, {distinct_k})"
    if k_gap == 1:
        return f"{base} + 1"
    return f"({base} * {k_gap}) + 1"


def _part_sql(dialect: str, n_parts: int, width: int, major_pct: int, n: int) -> str:
    if major_pct <= 0 or n_parts < 2:
        if dialect == "cockroach":
            return f"LEAST({n_parts}, div(i - 1, {width}) + 1)"
        if dialect == "clickhouse":
            return f"least({n_parts}, intDiv(i - 1, {width}) + 1)"
        if dialect in {"postgres", "opengauss"}:
            return f"LEAST({n_parts}, ((i - 1) / {width}) + 1)"
        return f"LEAST({n_parts}, FLOOR((i - 1) / {width}) + 1)"
    cutoff = max(1, (n * major_pct) // 100)
    rest = max(1, n - cutoff)
    width_rest = max(1, (rest + n_parts - 2) // (n_parts - 1))
    if dialect == "cockroach":
        inner = f"div(i - {cutoff} - 1, {width_rest})"
        return f"CASE WHEN i <= {cutoff} THEN 1 ELSE least({n_parts}, {inner} + 2) END"
    if dialect == "clickhouse":
        inner = f"intDiv(i - {cutoff} - 1, {width_rest})"
        return f"if(i <= {cutoff}, 1, least({n_parts}, {inner} + 2))"
    if dialect in {"postgres", "opengauss"}:
        inner = f"((i - {cutoff} - 1) / {width_rest})"
    else:
        inner = f"FLOOR((i - {cutoff} - 1) / {width_rest})"
    return f"CASE WHEN i <= {cutoff} THEN 1 ELSE LEAST({n_parts}, {inner} + 2) END"


def _payload_sql(dialect: str, null_stride: int) -> str:
    if dialect == "cockroach":
        dense = "mod(i * 17, 97)"
        if null_stride <= 0:
            return dense
        return f"CASE WHEN mod(i, {null_stride}) = 0 THEN NULL ELSE {dense} END"
    if dialect == "clickhouse":
        dense = "(i * 17) % 97"
        if null_stride <= 0:
            return dense
        return f"if(i % {null_stride} = 0, CAST(NULL AS Nullable(Int32)), {dense})"
    dense = "MOD(i * 17, 97)"
    if null_stride <= 0:
        return dense
    return f"CASE WHEN MOD(i, {null_stride}) = 0 THEN NULL ELSE {dense} END"


def _number_select(
    dialect: str, n: int, distinct_k: int, group: int, width: int, n_parts: int
) -> str:
    k_expr = f"MOD(i - 1, {distinct_k}) + 1"
    a_expr = f"FLOOR((i - 1) / {group}) + 1"
    # Keep a < b so CHECK (a < b) holds; rem in 0..group-1 keeps (a,b) unique.
    b_expr = f"({a_expr}) + MOD(i - 1, {group}) + 1"
    u0_expr = f"FLOOR((i - 1) / {U_PAIR}) + 1"
    u1_expr = f"MOD(i - 1, {U_PAIR}) + 1"
    payload_expr = "MOD(i * 17, 97)"
    if dialect == "cockroach":
        a_cr = f"div(i - 1, {group}) + 1"
        projection = (
            f"i, i, mod(i - 1, {distinct_k}) + 1, {a_cr}, "
            f"({a_cr}) + mod(i - 1, {group}) + 1, div(i - 1, {U_PAIR}) + 1, "
            f"mod(i - 1, {U_PAIR}) + 1, LEAST({n_parts}, div(i - 1, {width}) + 1), "
            f"mod(i * 17, 97)"
        )
        return f"SELECT {projection} FROM generate_series(1, {n}) AS g(i)"
    if dialect in {"postgres", "opengauss"}:
        part_expr = f"LEAST({n_parts}, ((i - 1) / {width}) + 1)"
        a_pg = f"((i - 1) / {group}) + 1"
        u0_pg = f"((i - 1) / {U_PAIR}) + 1"
        b_pg = f"({a_pg}) + MOD(i - 1, {group}) + 1"
        projection = (
            f"i, i, {k_expr}, {a_pg}, {b_pg}, "
            f"{u0_pg}, {u1_expr}, {part_expr}, {payload_expr}"
        )
        return f"SELECT {projection} FROM generate_series(1, {n}) AS g(i)"
    if dialect == "clickhouse":
        a_ch = f"intDiv(i - 1, {group}) + 1"
        projection = (
            f"i, i, (i - 1) % {distinct_k} + 1, {a_ch}, "
            f"({a_ch}) + (i - 1) % {group} + 1, intDiv(i - 1, {U_PAIR}) + 1, "
            f"(i - 1) % {U_PAIR} + 1, least({n_parts}, intDiv(i - 1, {width}) + 1), "
            f"(i * 17) % 97"
        )
        return f"SELECT {projection} FROM (SELECT number + 1 AS i FROM numbers({n}))"
    part_expr = f"LEAST({n_parts}, FLOOR((i - 1) / {width}) + 1)"
    projection = (
        f"i, i, {k_expr}, {a_expr}, {b_expr}, {u0_expr}, {u1_expr}, {part_expr}, {payload_expr}"
    )
    if dialect == "oracle":
        return (
            "SELECT "
            f"{projection} FROM (SELECT LEVEL AS i FROM dual CONNECT BY LEVEL <= {n})"
        )
    return (
        "WITH RECURSIVE seq(i) AS ("
        f"SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < {n}"
        f") SELECT {projection} FROM seq"
    )
