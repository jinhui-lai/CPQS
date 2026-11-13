from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

# CardinalityCertificate lives in oracle.py; imported lazily in type hints only
# would cycle through query → oracle → catalog. Keep certificates as plain list.


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    nullable: bool = False
    generated_as: Optional[str] = None  # e.g. ABS(k); None ⇒ ordinary column
    generated_stored: bool = True  # STORED/PERSISTENT vs VIRTUAL


@dataclass(frozen=True)
class ForeignKey:
    """Referential constraint used by join-elimination certificates."""

    name: str
    columns: Tuple[str, ...]
    ref_table: str
    ref_columns: Tuple[str, ...]


@dataclass(frozen=True)
class IndexPath:
    """Normalized access-path metadata for one physical/logical index.

    Optional fields support the Schema-Driven Access certificates:
    ``directions`` (ASC/DESC per key), ``expression`` (expression / generated
    column index), ``predicate`` (partial index WHERE), ``covering`` (index-only
    / INCLUDE). Hash and BRIN indexes set ``ordered=False``.
    """

    name: str
    table: str
    keys: Tuple[str, ...]
    unique: bool
    ordered: bool
    index_type: str
    primary: bool = False
    physical: bool = False
    inline: bool = True  # UNIQUE/PK in CREATE TABLE vs CREATE UNIQUE INDEX
    directions: Tuple[str, ...] = ()  # ASC/DESC per key; empty ⇒ all ASC
    expression: Optional[str] = None
    predicate: Optional[str] = None
    covering: bool = False

    def __post_init__(self) -> None:
        if self.directions and len(self.directions) != len(self.keys):
            raise ValueError("directions must match keys length")
        if self.index_type in {"hash", "brin"} and self.ordered:
            raise ValueError(f"{self.index_type} indexes are not ordered")

    def direction_of(self, column: str) -> str:
        if column not in self.keys:
            return "ASC"
        if not self.directions:
            return "ASC"
        return self.directions[self.keys.index(column)].upper()


@dataclass(frozen=True)
class PartitionSpec:
    """Partition metadata with known per-partition row membership.

    ``kind`` is ``range``, ``list``, or ``hash``.
    RANGE: partition ``p`` (1-based) holds values in ``[p, p+1)``.
    LIST: partition ``p`` holds the singleton value ``p``.
    HASH: generator uses ``value % n_parts`` (0 → partition ``n_parts``) so
    ``P'`` is computed from data, not optimizer stats.
    """

    column: str
    n_parts: int
    width: int
    kind: str = "range"
    list_values: Tuple[Tuple[int, ...], ...] = ()  # per-partition value lists


@dataclass
class TableData:
    name: str
    columns: List[Column]
    indexes: List[IndexPath]
    pk_column: str
    uk_column: Optional[str] = None
    profile_name: str = "default"
    pk_keys: Tuple[str, ...] = ()
    partitioned: bool = False
    partition: Optional[PartitionSpec] = None
    checks: List[str] = field(default_factory=list)
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    extra_ddl: List[str] = field(default_factory=list)
    post_load_ddl: List[str] = field(default_factory=list)  # MVs etc. after INSERT
    certificates: List[Any] = field(default_factory=list)
    mv_name: Optional[str] = None
    mv_cardinality: Optional[int] = None  # N_mv when a rewrite certificate applies
    # Mirrors SchemaProfile distribution knobs so INSERT..SELECT stays in sync.
    k_gap: int = 1
    null_stride: int = 0
    part_major_pct: int = 0
    rows: List[Dict[str, Any]] = field(default_factory=list)
    value_count: Dict[str, Dict[Any, int]] = field(default_factory=dict)
    value_rows: Dict[str, Dict[Any, List[int]]] = field(default_factory=dict)
    ordered_values: Dict[str, List[Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pk_keys:
            self.pk_keys = (self.pk_column,)

    @property
    def n(self) -> int:
        return len(self.rows)

    def add_row(self, row: Dict[str, Any]) -> None:
        pk = int(row[self.pk_column])
        self.rows.append(row)
        for col in row:
            self.value_count.setdefault(col, defaultdict(int))
            self.value_rows.setdefault(col, defaultdict(list))
            self.value_count[col][row[col]] += 1
            self.value_rows[col][row[col]].append(pk)

    def freeze(self) -> None:
        for col, mapping in self.value_count.items():
            self.value_count[col] = dict(mapping)
        for col, mapping in self.value_rows.items():
            self.value_rows[col] = {k: list(v) for k, v in mapping.items()}
        for col, mapping in self.value_count.items():
            self.ordered_values[col] = sorted(mapping.keys(), key=lambda v: (v is None, v))

    def existing_values(self, column: str) -> List[Any]:
        return list(self.ordered_values.get(column, []))

    def multiplicity(self, column: str, value: Any) -> int:
        return self.value_count.get(column, {}).get(value, 0)

    def rows_for(self, column: str, value: Any) -> FrozenSet[int]:
        return frozenset(self.value_rows.get(column, {}).get(value, ()))

    def rows_in_range(self, column: str, low: Any, high: Any) -> FrozenSet[int]:
        out: List[int] = []
        for value, pks in self.value_rows.get(column, {}).items():
            if low <= value <= high:
                out.extend(pks)
        return frozenset(out)

    def pk_set(self) -> FrozenSet[int]:
        return frozenset(int(r[self.pk_column]) for r in self.rows)

    def row_by_pk(self, pk: int) -> Optional[Dict[str, Any]]:
        for row in self.rows:
            if int(row[self.pk_column]) == pk:
                return row
        return None

    def unique_leading_columns(self) -> List[str]:
        cols: List[str] = []
        for idx in self.indexes:
            if idx.unique and len(idx.keys) == 1 and idx.keys[0] not in cols:
                cols.append(idx.keys[0])
        return cols

    def composite_unique_indexes(self) -> List[IndexPath]:
        return [idx for idx in self.indexes if idx.unique and len(idx.keys) >= 2]

    def pair_for_pk(self, pk: int, cols: Sequence[str]) -> Optional[Tuple[Any, ...]]:
        row = self.row_by_pk(pk)
        if row is None:
            return None
        return tuple(row[c] for c in cols)

    def rows_for_pair(self, cols: Sequence[str], values: Sequence[Any]) -> FrozenSet[int]:
        if not cols:
            return frozenset()
        rows = self.rows_for(cols[0], values[0])
        if len(cols) == 1:
            return rows
        out: List[int] = []
        for pk in rows:
            row = self.row_by_pk(pk)
            if row is None:
                continue
            if all(row[c] == v for c, v in zip(cols[1:], values[1:])):
                out.append(pk)
        return frozenset(out)

    def rows_in_partition(self, part_value: int) -> FrozenSet[int]:
        if self.partition is None:
            return frozenset()
        return self.rows_for(self.partition.column, part_value)

    def unique_indexes(self) -> List[IndexPath]:
        return [idx for idx in self.indexes if idx.unique]

    def ordered_indexes(self) -> List[IndexPath]:
        return [idx for idx in self.indexes if idx.ordered]

    def leading_index_for(self, column: str) -> Optional[IndexPath]:
        for idx in self.indexes:
            if idx.keys and idx.keys[0] == column:
                return idx
        return None

    def compatible_ordered_index(self, column: str, prefix_eq: Sequence[str] = ()) -> Optional[IndexPath]:
        """True when ``column`` is the leading key, or preceding keys are equality-bound."""
        for idx in self.indexes:
            if not idx.ordered:
                continue
            if idx.keys[0] == column and not prefix_eq:
                return idx
            if column not in idx.keys:
                continue
            pos = idx.keys.index(column)
            preceding = idx.keys[:pos]
            if list(preceding) == list(prefix_eq):
                return idx
        return None

    def leading_desc_index(self, column: str) -> Optional[IndexPath]:
        """B-tree whose leading key is ``column DESC`` (or explicit DESC direction)."""
        for idx in self.indexes:
            if not idx.ordered or not idx.keys or idx.keys[0] != column:
                continue
            if idx.direction_of(column) == "DESC":
                return idx
        return None

    def not_null_columns(self) -> List[str]:
        return [c.name for c in self.columns if not c.nullable]

    def base_columns(self) -> List[Column]:
        """Columns that must appear in INSERT (excludes generated columns)."""
        return [c for c in self.columns if not c.generated_as]

    def expression_indexes(self) -> List[IndexPath]:
        return [idx for idx in self.indexes if idx.expression]

    def partial_indexes(self) -> List[IndexPath]:
        return [idx for idx in self.indexes if idx.predicate]

    def covering_indexes(self) -> List[IndexPath]:
        return [idx for idx in self.indexes if idx.covering]

    def hash_partition_id(self, value: int) -> int:
        """Generator-side HASH mapping used by PartitionSpec(kind='hash')."""
        if self.partition is None or self.partition.kind != "hash":
            raise ValueError("table is not HASH-partitioned")
        n = self.partition.n_parts
        return (int(value) % n) or n


@dataclass
class Catalog:
    tables: Dict[str, TableData] = field(default_factory=dict)
    seed: int = 1
    n_primary: int = 0
    n_secondary: int = 0
    n_tables: int = 2
    profile_name: str = "default"

    def add(self, table: TableData) -> None:
        table.freeze()
        self.tables[table.name] = table

    def primary(self) -> TableData:
        return self.tables["t0"]

    def secondary(self) -> TableData:
        return self.tables["t1"]

    def ordered_tables(self) -> List[TableData]:
        return [self.tables[k] for k in sorted(self.tables, key=lambda n: int(n[1:]))]

    def n_rows(self) -> int:
        return sum(t.n for t in self.tables.values())

    def table(self, name: str) -> TableData:
        return self.tables[name]

    def max_n(self) -> int:
        return max((t.n for t in self.tables.values()), default=0)


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def in_list(values: Iterable[Any]) -> str:
    rendered = ", ".join(sql_literal(v) for v in values)
    return f"({rendered})"
