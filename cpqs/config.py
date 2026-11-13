from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple


MIN_ROWS = 10000


@dataclass
class Config:
    db: str = "mysql"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "cpqs"
    oracle_service: str = "FREEPDB1"
    rows: int = MIN_ROWS
    rows_small: int = 0  # 0 = same as rows; every table is at least MIN_ROWS
    queries: int = 800
    queries_per_state: int = 400
    stale_queries: int = 80
    max_states: int = 8
    seed: int = 1
    max_cardinality_ratio: float = 0.01
    overestimate_factor: float = 8.0
    distinct_k: int = 100
    n_tables: int = 4
    poc_dir: str = "pocs"
    analyze: bool = True
    timeout_s: int = 30
    skip_kinds: List[str] = field(default_factory=list)
    only_kinds: List[str] = field(default_factory=list)
    dry_run: bool = False
    allow_small_rows: bool = False

    def table_row_counts(self) -> Tuple[int, int]:
        n0 = self.rows
        n1 = self.rows_small if self.rows_small > 0 else self.rows
        if not self.allow_small_rows:
            n0 = max(n0, MIN_ROWS)
            n1 = max(n1, MIN_ROWS)
        return n0, n1


DEFAULT_PORTS = {
    "mysql": 3306,
    "mariadb": 3307,
    "tidb": 4000,
    "postgres": 5432,
    "cockroach": 26257,
    "oracle": 1521,
    "clickhouse": 8123,
    "opengauss": 15432,
    "oceanbase": 2881,
}

DEFAULT_USERS = {
    "mysql": "root",
    "mariadb": "root",
    "tidb": "root",
    "postgres": "postgres",
    "cockroach": "root",
    "oracle": "system",
    "clickhouse": "default",
    "opengauss": "gaussdb",
    "oceanbase": "root",
}

DEFAULT_PASSWORDS = {
    "mysql": "",
    "mariadb": "",
    "tidb": "",
    "postgres": "postgres",
    "cockroach": "",
    "oracle": "oracle",
    "clickhouse": "",
    "opengauss": "OpenGauss@123",
    "oceanbase": "",
}
