from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from cpqs.core.catalog import Catalog, Column, ForeignKey, IndexPath, PartitionSpec, TableData
from cpqs.core.oracle import CardinalityCertificate
from cpqs.gen.data import assign_part, k_of, payload_of

U_PAIR = 10  # (u0, u1) with u1 in 1..10 is unique

# Dialects that accept CREATE INDEX ... (col DESC).
DESC_INDEX_DBS = frozenset(
    {"mysql", "mariadb", "tidb", "postgres", "cockroach", "oracle", "opengauss", "oceanbase"}
)
LIST_PARTITION_DBS = frozenset({"mysql", "mariadb", "tidb", "oracle", "oceanbase", "opengauss"})
HASH_PARTITION_DBS = frozenset({"mysql", "mariadb", "tidb", "oracle", "oceanbase", "opengauss"})
# Direct expression / functional indexes (MariaDB uses generated columns instead).
EXPRESSION_INDEX_DBS = frozenset(
    {"mysql", "tidb", "postgres", "cockroach", "oracle", "opengauss", "oceanbase"}
)
GENERATED_COLUMN_DBS = frozenset(
    {"mysql", "mariadb", "tidb", "postgres", "cockroach", "oracle", "opengauss", "oceanbase"}
)
PARTIAL_INDEX_DBS = frozenset({"postgres", "cockroach", "opengauss", "tidb"})
COVERING_INCLUDE_DBS = frozenset({"postgres", "cockroach", "opengauss"})
FK_DBS = frozenset(
    {"mysql", "mariadb", "tidb", "postgres", "cockroach", "oracle", "opengauss", "oceanbase"}
)
# Index Merge OR/AND is primarily a MySQL-family access path we can observe.
INDEX_MERGE_DBS = frozenset({"mysql", "mariadb", "tidb", "oceanbase"})
# InnoDB has no hash indexes. Postgres hash indexes are equality-only.
HASH_INDEX_DBS = frozenset({"postgres", "opengauss"})


@dataclass(frozen=True)
class SchemaProfile:
    """One database-state template: constraint style + column types."""

    name: str
    pk_name: str = "c0"
    uk_name: str = "c1"
    pk_type: str = "INT"
    uk_type: str = "INT"
    k_type: str = "INT"
    unique_as: str = "constraint"  # constraint | unique_index
    composite_ab: bool = True
    pk_keys: Tuple[str, ...] = ("c0",)
    extra_unique: Tuple[Tuple[str, ...], ...] = ()
    extra_unique_as: str = "unique_index"
    partitioned: bool = False
    n_parts: int = 3
    partition_kind: str = "range"  # range | list | hash
    desc_index_on: Tuple[str, ...] = ()  # leading DESC secondary indexes
    expression_abs_k: bool = False  # INDEX on ABS(k) / generated k_abs
    partial_on_k: bool = False  # INDEX(c0) WHERE k <= threshold
    covering_include: bool = False  # INDEX(k) INCLUDE (payload)
    with_fk: bool = False  # t{i>0}.c0 REFERENCES t0(c0)
    dual_secondary: bool = False  # INDEX(a) and INDEX(b) for Index Merge
    materialized_by_k: bool = False  # aggregate MV on k (Oracle rewrite)
    hash_payload: bool = False  # USING HASH on payload; equality only, no range certificate
    # Data-distribution knobs. Defaults reproduce the dense, non-null, even layout.
    k_gap: int = 1  # step between successive k values; 1 keeps k in 1..distinct_k
    null_stride: int = 0  # 0 = payload NOT NULL; else NULL when i % stride == 0
    part_major_pct: int = 0  # 0 = even stripes; else this percent of rows sit in partition 1


def schema_profiles(dialect: str) -> List[SchemaProfile]:
    """Rotate through these after a state stops finding new bug kinds."""
    int_t, big_t, dec_t = _numeric_types(dialect)
    return [
        SchemaProfile("pk_int_unique_constraint", pk_type=int_t, uk_type=int_t, unique_as="constraint"),
        SchemaProfile("pk_bigint_unique_index", pk_type=big_t, uk_type=big_t, unique_as="unique_index"),
        SchemaProfile(
            "composite_uk_ab_index",
            pk_type=int_t,
            uk_type=int_t,
            extra_unique=(("a", "b"),),
            extra_unique_as="unique_index",
        ),
        SchemaProfile(
            "composite_uk_u0u1_constraint",
            pk_type=int_t,
            uk_type=big_t,
            extra_unique=(("u0", "u1"),),
            extra_unique_as="constraint",
        ),
        SchemaProfile(
            "composite_pk_ab",
            pk_type=int_t,
            uk_type=int_t,
            pk_keys=("a", "b"),
            unique_as="constraint",
        ),
        SchemaProfile(
            "composite_pk_u0u1",
            pk_type=big_t,
            uk_type=int_t,
            pk_keys=("u0", "u1"),
            unique_as="unique_index",
        ),
        SchemaProfile(
            "range_part_int",
            pk_type=int_t,
            uk_type=int_t,
            partitioned=True,
            n_parts=3,
            unique_as="constraint",
        ),
        SchemaProfile(
            "range_part_bigint_uk_index",
            pk_type=big_t,
            uk_type=big_t,
            partitioned=True,
            n_parts=3,
            extra_unique=(("u0", "u1"),),
            extra_unique_as="unique_index",
            unique_as="unique_index",
        ),
        SchemaProfile(
            "list_part_int",
            pk_type=int_t,
            uk_type=int_t,
            partitioned=True,
            n_parts=3,
            partition_kind="list",
            unique_as="constraint",
        ),
        SchemaProfile(
            "hash_part_int",
            pk_type=int_t,
            uk_type=int_t,
            partitioned=True,
            n_parts=3,
            partition_kind="hash",
            unique_as="constraint",
        ),
        SchemaProfile(
            "desc_k_index",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="constraint",
            desc_index_on=("k",),
        ),
        SchemaProfile(
            "desc_pk_secondary",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="unique_index",
            desc_index_on=("c0",),
        ),
        SchemaProfile(
            "expr_abs_k",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="constraint",
            expression_abs_k=True,
        ),
        SchemaProfile(
            "partial_pk_on_k",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="constraint",
            partial_on_k=True,
        ),
        SchemaProfile(
            "covering_k_payload",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="constraint",
            covering_include=True,
        ),
        SchemaProfile(
            "fk_chain",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="constraint",
            with_fk=True,
        ),
        SchemaProfile(
            "dual_ab_indexes",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="constraint",
            dual_secondary=True,
            composite_ab=False,
        ),
        SchemaProfile(
            "mv_group_k",
            pk_type=int_t,
            uk_type=int_t,
            unique_as="constraint",
            materialized_by_k=True,
        ),
        SchemaProfile("pk_int_uk_bigint_constraint", pk_type=int_t, uk_type=big_t, unique_as="constraint"),
        SchemaProfile("pk_decimal_unique_constraint", pk_type=dec_t, uk_type=dec_t, unique_as="constraint"),
        SchemaProfile("pk_int_uk_decimal_constraint", pk_type=int_t, uk_type=dec_t, unique_as="constraint"),
        SchemaProfile("pk_bigint_unique_constraint", pk_type=big_t, uk_type=big_t, unique_as="constraint"),
    ]


def _numeric_types(dialect: str) -> tuple[str, str, str]:
    if dialect == "oracle":
        return "NUMBER(10)", "NUMBER(18)", "NUMBER(18,0)"
    if dialect in {"postgres", "cockroach", "opengauss"}:
        return "INTEGER", "BIGINT", "NUMERIC(18,0)"
    if dialect == "clickhouse":
        return "Int32", "Int64", "Decimal(18, 0)"
    return "INT", "BIGINT", "DECIMAL(18,0)"


def build_catalog(
    n_primary: int,
    n_secondary: int,
    distinct_k: int,
    profile: Optional[SchemaProfile] = None,
    dialect: str = "mysql",
    n_tables: int = 2,
) -> Catalog:
    if n_primary <= 0 or n_secondary <= 0:
        raise ValueError("row counts must be positive")
    if distinct_k <= 0 or n_primary < distinct_k:
        raise ValueError("distinct_k must be in 1..rows")
    n_tables = max(2, min(4, n_tables))
    if profile is None:
        profile = schema_profiles(dialect)[0]

    catalog = Catalog(
        seed=0,
        n_primary=n_primary,
        n_secondary=n_secondary,
        n_tables=n_tables,
        profile_name=profile.name,
    )
    for i in range(n_tables):
        dk = max(1, min(distinct_k, n_primary))
        catalog.add(_fill_table(f"t{i}", n_primary, dk, profile, dialect))
    catalog.n_secondary = n_primary
    return catalog


def _partition_allowed(kind: str, dialect: str) -> bool:
    if dialect == "cockroach":
        return False
    if kind == "range":
        # ClickHouse uses PARTITION BY col on MergeTree (no RANGE bounds DDL).
        return dialect in {
            "mysql",
            "mariadb",
            "tidb",
            "postgres",
            "oracle",
            "oceanbase",
            "opengauss",
            "clickhouse",
        }
    if kind == "list":
        return dialect in LIST_PARTITION_DBS
    if kind == "hash":
        return dialect in HASH_PARTITION_DBS
    return False


def _fill_table(name: str, n: int, distinct_k: int, profile: SchemaProfile, dialect: str) -> TableData:
    pk, uk = profile.pk_name, profile.uk_name
    n_parts = max(2, profile.n_parts)
    width = max(1, (n + n_parts - 1) // n_parts)
    part_kind = profile.partition_kind if profile.partitioned else "range"
    partitioned = bool(profile.partitioned) and _partition_allowed(part_kind, dialect)
    list_values = tuple((p,) for p in range(1, n_parts + 1)) if part_kind == "list" else ()
    part_spec = PartitionSpec(
        column="part",
        n_parts=n_parts,
        width=width,
        kind=part_kind if partitioned else "range",
        list_values=list_values,
    )

    pk_keys = profile.pk_keys
    uk_keys: Tuple[str, ...] = (uk,)
    extra_unique = list(profile.extra_unique)
    if partitioned and dialect in {"mysql", "mariadb", "tidb", "postgres", "oceanbase", "opengauss"}:
        if "part" not in pk_keys:
            pk_keys = pk_keys + ("part",)
        if "part" not in uk_keys:
            uk_keys = uk_keys + ("part",)
        extra_unique = [keys if "part" in keys else keys + ("part",) for keys in extra_unique]

    columns = [
        Column(pk, profile.pk_type, nullable=False),
        Column(uk, profile.uk_type, nullable=False),
        Column("k", profile.k_type, nullable=False),
        Column("a", "INT", nullable=False),
        Column("b", "INT", nullable=False),
        Column("u0", "INT", nullable=False),
        Column("u1", "INT", nullable=False),
        Column("part", "INT", nullable=False),
        Column("payload", "INT", nullable=profile.null_stride > 0),
    ]
    unique_inline = profile.unique_as == "constraint"
    extra_inline = profile.extra_unique_as == "constraint"
    indexes = [
        IndexPath("pk", name, pk_keys, True, True, "btree", primary=True, inline=True),
    ]
    # Single-column UNIQUE on c0 when it is not already the full PK.
    if pk_keys != (pk,) and not partitioned:
        indexes.append(
            IndexPath(f"{name}_{pk}", name, (pk,), True, True, "btree", inline=unique_inline)
        )
    indexes.append(
        IndexPath(
            f"{name}_{uk}",
            name,
            uk_keys,
            True,
            True,
            "btree",
            inline=unique_inline,
        )
    )
    indexes.append(IndexPath(f"{name}_k", name, ("k",), False, True, "btree", inline=False))
    if profile.composite_ab:
        already = any(idx.keys[:2] == ("a", "b") and idx.unique for idx in indexes)
        if not already:
            indexes.append(IndexPath(f"{name}_ab", name, ("a", "b"), False, True, "btree", inline=False))
    for i, keys in enumerate(extra_unique):
        indexes.append(
            IndexPath(
                f"{name}_cu{i}",
                name,
                tuple(keys),
                True,
                True,
                "btree",
                inline=extra_inline,
            )
        )
    if dialect in DESC_INDEX_DBS:
        for col in profile.desc_index_on:
            indexes.append(
                IndexPath(
                    f"{name}_{col}_desc",
                    name,
                    (col,),
                    unique=False,
                    ordered=True,
                    index_type="btree",
                    inline=False,
                    directions=("DESC",),
                )
            )

    # ABS(k) expression index, or MariaDB-style generated column + index.
    abs_expr = "ABS(k)"
    if profile.expression_abs_k and dialect in GENERATED_COLUMN_DBS:
        if dialect in EXPRESSION_INDEX_DBS and dialect != "mariadb":
            indexes.append(
                IndexPath(
                    f"{name}_abs_k",
                    name,
                    (),
                    unique=False,
                    ordered=True,
                    index_type="btree",
                    inline=False,
                    expression=abs_expr,
                )
            )
        else:
            # MariaDB (and fallback): generated column, then ordinary index.
            columns.append(
                Column(
                    "k_abs",
                    profile.k_type,
                    nullable=False,
                    generated_as=abs_expr,
                    generated_stored=True,
                )
            )
            indexes.append(
                IndexPath(
                    f"{name}_k_abs",
                    name,
                    ("k_abs",),
                    unique=False,
                    ordered=True,
                    index_type="btree",
                    inline=False,
                    expression=abs_expr,
                )
            )

    # Partial index: INDEX(pk) WHERE k <= ceil(distinct_k/2).
    partial_threshold = max(1, (distinct_k + 1) // 2)
    if profile.partial_on_k and dialect in PARTIAL_INDEX_DBS and not partitioned:
        indexes.append(
            IndexPath(
                f"{name}_pk_partial",
                name,
                (pk,),
                unique=False,
                ordered=True,
                index_type="btree",
                inline=False,
                predicate=f"k <= {partial_threshold}",
            )
        )

    if profile.covering_include and dialect in COVERING_INCLUDE_DBS:
        indexes.append(
            IndexPath(
                f"{name}_k_cover",
                name,
                ("k",),
                unique=False,
                ordered=True,
                index_type="btree",
                inline=False,
                covering=True,
            )
        )

    if profile.dual_secondary:
        indexes.append(IndexPath(f"{name}_a", name, ("a",), False, True, "btree", inline=False))
        indexes.append(IndexPath(f"{name}_b", name, ("b",), False, True, "btree", inline=False))
    if profile.hash_payload and dialect in HASH_INDEX_DBS:
        # Equality-only structure. ordered=False so range and ORDER BY certificates
        # cannot attach to it. No access bound is issued for this index.
        indexes.append(
            IndexPath(
                f"{name}_payload_hash",
                name,
                ("payload",),
                unique=False,
                ordered=False,
                index_type="hash",
                inline=False,
            )
        )

    foreign_keys: List[ForeignKey] = []
    if profile.with_fk and dialect in FK_DBS and name != "t0":
        foreign_keys.append(
            ForeignKey(
                name=f"{name}_fk_t0",
                columns=(pk,),
                ref_table="t0",
                ref_columns=(pk,),
            )
        )

    # Domain CHECKs on non-unique columns (k, payload) plus multi-column
    # inequalities. Rows are filled to satisfy every predicate below.
    k_gap = max(1, profile.k_gap)
    max_k = (distinct_k - 1) * k_gap + 1
    # ``k <= pk`` holds for the dense numbering. A spread k can exceed the
    # row's own primary key, so that conjunct is dropped rather than made false.
    checks: List[str] = [
        f"k >= 1 AND k <= {max_k}",
        "payload >= 0 AND payload <= 96",
    ]
    if k_gap == 1:
        checks.append(f"k <= {pk}")
    checks.append("a < b")
    certificates = _base_certificates(name, pk, dialect, partitioned, part_kind)
    if profile.expression_abs_k:
        certificates.append(
            CardinalityCertificate(
                mechanism="unique lookup",
                unit="index entry",
                bound=0,
                table=name,
                index=f"{name}_abs_k",
                precondition=f"query predicate matches {abs_expr}",
                dialects=(dialect,),
                priority="P0",
            )
        )
    if profile.partial_on_k:
        certificates.append(
            CardinalityCertificate(
                mechanism="unique lookup",
                unit="index entry",
                bound=0,
                table=name,
                index=f"{name}_pk_partial",
                precondition=f"query implies k <= {partial_threshold}",
                dialects=(dialect,),
                priority="P0",
            )
        )
    table = TableData(
        name=name,
        columns=columns,
        indexes=indexes,
        pk_column=pk,
        uk_column=uk,
        profile_name=profile.name,
        pk_keys=pk_keys,
        partitioned=partitioned,
        # Keep PartitionSpec even when DDL is not partitioned so helpers like
        # rows_in_partition still see the generated ``part`` column stripes.
        partition=part_spec,
        checks=checks if dialect != "clickhouse" else [],
        foreign_keys=foreign_keys,
        certificates=certificates,
        k_gap=k_gap,
        null_stride=max(0, profile.null_stride),
        part_major_pct=max(0, profile.part_major_pct),
    )
    # Stash partial threshold for generators (via profile name + recomputation).
    table.extra_ddl = []  # filled after rows; placeholder keeps attribute writable
    group = max(1, n // distinct_k)
    for i in range(1, n + 1):
        k = k_of(i, distinct_k, k_gap)
        a = ((i - 1) // group) + 1
        # b = a + rem + 1 ⇒ a < b always, and (a, b) stays unique for rem in 0..group-1.
        b = a + ((i - 1) % group) + 1
        u0 = ((i - 1) // U_PAIR) + 1
        u1 = ((i - 1) % U_PAIR) + 1
        # RANGE/LIST: stripe by width into part ∈ {1..n_parts}.
        # HASH: same part values; prune uses value % n_parts (generator-side).
        # part_major_pct packs that share of rows into partition 1.
        part = assign_part(i, n, n_parts, width, profile.part_major_pct)
        row = {
            pk: i,
            uk: i,
            "k": k,
            "a": a,
            "b": b,
            "u0": u0,
            "u1": u1,
            "part": part,
            "payload": payload_of(i, profile.null_stride),
        }
        # Mirror generated ABS(k) in-memory so oracles can use rows_for("k_abs", ...).
        if any(c.name == "k_abs" for c in columns):
            row["k_abs"] = abs(k)
        table.add_row(row)
    table.extra_ddl = _partition_child_sql(table, dialect)
    if profile.materialized_by_k and dialect == "oracle" and name == "t0":
        mv = f"mv_{name}_k"
        # Drop+create after INSERT so BUILD IMMEDIATE sees populated base table.
        table.post_load_ddl = [
            f"BEGIN EXECUTE IMMEDIATE 'DROP MATERIALIZED VIEW {mv}'; "
            f"EXCEPTION WHEN OTHERS THEN NULL; END;",
            f"CREATE MATERIALIZED VIEW {mv} "
            f"BUILD IMMEDIATE REFRESH COMPLETE ON DEMAND ENABLE QUERY REWRITE "
            f"AS SELECT k, COUNT(*) AS cnt FROM {name} GROUP BY k",
        ]
        table.mv_name = mv
        table.mv_cardinality = distinct_k
        table.certificates.append(
            CardinalityCertificate(
                mechanism="rewrite",
                unit="mv row",
                bound=distinct_k,
                table=name,
                index=mv,
                precondition="aggregate query fully answered by MV",
                dialects=("oracle",),
                priority="P1",
            )
        )
    return table


def _base_certificates(
    table: str,
    pk: str,
    dialect: str,
    partitioned: bool,
    part_kind: str,
) -> List[CardinalityCertificate]:
    certs = [
        CardinalityCertificate(
            mechanism="unique lookup",
            unit="row",
            bound=1,
            table=table,
            index="pk",
            precondition=f"{pk} exists and is unique",
            dialects=(dialect,),
            priority="P0",
        ),
        CardinalityCertificate(
            mechanism="contradiction",
            unit="row",
            bound=0,
            table=table,
            precondition=f"{pk} IS NOT NULL ⇒ {pk} IS NULL is empty",
            dialects=(dialect,),
            priority="P0",
        ),
        CardinalityCertificate(
            mechanism="join elimination",
            unit="row",
            bound=0,
            table=table,
            precondition="LEFT JOIN on unique key unused in SELECT ⇒ right access 0",
            dialects=(dialect,),
            priority="P0",
        ),
    ]
    if partitioned:
        certs.append(
            CardinalityCertificate(
                mechanism="partition prune",
                unit="partition row",
                bound=0,
                table=table,
                precondition=f"{part_kind} partition predicate selects known P'",
                dialects=(dialect,),
                priority="P0",
            )
        )
    return certs


def _clickhouse_type(sql_type: str) -> str:
    compact = sql_type.upper().replace(" ", "")
    if compact.startswith("DECIMAL") or compact.startswith("NUMERIC"):
        return "Decimal(18, 0)"
    if compact in {"BIGINT", "INT64"}:
        return "Int64"
    if compact.startswith("INT"):
        return "Int32"
    return sql_type


def _column_ddl(col: Column, dialect: str) -> str:
    null_sql = "" if col.nullable else " NOT NULL"
    if not col.generated_as:
        return f"{col.name} {col.sql_type}{null_sql}"
    expr = col.generated_as
    if dialect == "oracle":
        # Oracle virtual column: col AS (expr) [VIRTUAL]
        return f"{col.name} {col.sql_type} GENERATED ALWAYS AS ({expr}) VIRTUAL"
    if dialect in {"postgres", "opengauss"}:
        return f"{col.name} {col.sql_type} GENERATED ALWAYS AS ({expr}) STORED"
    if dialect == "cockroach":
        return f"{col.name} {col.sql_type} AS ({expr}) STORED"
    if dialect == "mariadb":
        # MariaDB 10.2+ accepts STORED; NOT NULL after the generated clause is
        # a syntax error (ERROR 1064 near 'NOT NULL').
        kind = "STORED" if col.generated_stored else "VIRTUAL"
        return f"{col.name} {col.sql_type} AS ({expr}) {kind}"
    # MySQL / TiDB / OceanBase
    kind = "STORED" if col.generated_stored else "VIRTUAL"
    return f"{col.name} {col.sql_type} GENERATED ALWAYS AS ({expr}) {kind}{null_sql}"


def create_table_sql(table: TableData, dialect: str = "mysql") -> str:
    if dialect == "clickhouse":
        cols = ", ".join(
            f"{c.name} "
            + (
                f"Nullable({_clickhouse_type(c.sql_type)})"
                if c.nullable
                else _clickhouse_type(c.sql_type)
            )
            for c in table.columns
            if not c.generated_as
        )
        pk_keys = next((idx.keys for idx in table.indexes if idx.primary), ("c0",))
        engine = ["ENGINE = MergeTree"]
        if table.partitioned and table.partition is not None:
            engine.append(f"PARTITION BY {table.partition.column}")
        engine.append(f"ORDER BY ({', '.join(pk_keys)})")
        return f"CREATE TABLE {table.name} ({cols}) {' '.join(engine)}"
    cols = ", ".join(_column_ddl(c, dialect) for c in table.columns)
    extras: List[str] = []
    for idx in table.indexes:
        if idx.primary:
            extras.append(f"PRIMARY KEY ({', '.join(idx.keys)})")
        elif idx.unique and idx.inline:
            extras.append(f"UNIQUE ({', '.join(idx.keys)})")
    for chk in table.checks:
        extras.append(f"CHECK ({chk})")
    for fk in table.foreign_keys:
        extras.append(
            f"CONSTRAINT {fk.name} FOREIGN KEY ({', '.join(fk.columns)}) "
            f"REFERENCES {fk.ref_table} ({', '.join(fk.ref_columns)})"
        )
    extra_sql = (", " + ", ".join(extras)) if extras else ""
    body = f"CREATE TABLE {table.name} ({cols}{extra_sql})"
    part = _partition_clause(table, dialect)
    return f"{body} {part}".rstrip() if part else body


def _partition_clause(table: TableData, dialect: str) -> str:
    if not table.partitioned or table.partition is None:
        return ""
    spec = table.partition
    col = spec.column
    kind = spec.kind
    if dialect == "postgres":
        if kind != "range":
            return ""
        return f"PARTITION BY RANGE ({col})"
    if dialect not in {"mysql", "mariadb", "tidb", "oracle", "oceanbase", "opengauss"}:
        return ""
    if kind == "list":
        parts = []
        for p in range(1, spec.n_parts + 1):
            vals = spec.list_values[p - 1] if spec.list_values else (p,)
            joined = ", ".join(str(v) for v in vals)
            # Oracle/OpenGauss LIST uses VALUES (...); MySQL-family uses VALUES IN (...).
            if dialect in {"oracle", "opengauss"}:
                parts.append(f"PARTITION p{p} VALUES ({joined})")
            else:
                parts.append(f"PARTITION p{p} VALUES IN ({joined})")
        # Oracle LIST needs a DEFAULT partition for out-of-list inserts; MySQL-family
        # rejects unknown list values at DML time, so no catch-all is required.
        if dialect in {"oracle", "opengauss"}:
            parts.append("PARTITION pdef VALUES (DEFAULT)")
        return f"PARTITION BY LIST ({col}) ({', '.join(parts)})"
    if kind == "hash":
        if dialect in {"oracle", "opengauss"}:
            parts = [f"PARTITION p{p}" for p in range(1, spec.n_parts + 1)]
            return f"PARTITION BY HASH ({col}) ({', '.join(parts)})"
        return f"PARTITION BY HASH({col}) PARTITIONS {spec.n_parts}"
    # RANGE (default)
    parts = []
    for p in range(1, spec.n_parts + 1):
        parts.append(f"PARTITION p{p} VALUES LESS THAN ({p + 1})")
    if dialect in {"oracle", "opengauss"}:
        parts.append("PARTITION pmax VALUES LESS THAN (MAXVALUE)")
    else:
        parts.append("PARTITION pmax VALUES LESS THAN MAXVALUE")
    return f"PARTITION BY RANGE ({col}) ({', '.join(parts)})"


def _partition_child_sql(table: TableData, dialect: str) -> List[str]:
    if not table.partitioned or table.partition is None or dialect != "postgres":
        return []
    spec = table.partition
    if spec.kind != "range":
        return []
    stmts = []
    for p in range(1, spec.n_parts + 1):
        stmts.append(
            f"CREATE TABLE {table.name}_p{p} PARTITION OF {table.name} "
            f"FOR VALUES FROM ({p}) TO ({p + 1})"
        )
    return stmts


def _index_key_sql(idx: IndexPath, dialect: str) -> str:
    if idx.expression and not idx.keys:
        # Functional / expression index: dialect-specific parentheses.
        expr = idx.expression
        if dialect in {"mysql", "tidb", "oceanbase", "postgres", "opengauss"}:
            return f"({expr})"  # CREATE INDEX ... ON t ((ABS(k)))
        return expr  # Oracle / Cockroach: ON t (ABS(k))
    parts = []
    for i, key in enumerate(idx.keys):
        direction = ""
        if idx.directions and idx.directions[i].upper() == "DESC":
            direction = " DESC"
        parts.append(f"{key}{direction}")
    return ", ".join(parts)


def _covering_suffix(idx: IndexPath, dialect: str) -> str:
    if not idx.covering or dialect not in COVERING_INCLUDE_DBS:
        return ""
    # INCLUDE list is fixed to payload for the covering_k_payload profile.
    return " INCLUDE (payload)"


def secondary_index_sql(table: TableData, dialect: str = "mysql") -> List[str]:
    if dialect == "clickhouse":
        return []
    stmts = []
    for idx in table.indexes:
        if idx.primary or idx.inline:
            continue
        if idx.predicate and dialect not in PARTIAL_INDEX_DBS:
            continue
        if idx.expression and not idx.keys and dialect not in EXPRESSION_INDEX_DBS:
            continue
        keys = _index_key_sql(idx, dialect)
        if not keys:
            continue
        if idx.index_type == "hash" and dialect not in HASH_INDEX_DBS:
            continue
        unique = "UNIQUE " if idx.unique else ""
        using = " USING HASH" if idx.index_type == "hash" else ""
        where = f" WHERE {idx.predicate}" if idx.predicate else ""
        cover = _covering_suffix(idx, dialect)
        stmts.append(f"CREATE {unique}INDEX {idx.name} ON {table.name}{using} ({keys}){cover}{where}")
    return stmts
