from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Sequence

from cpqs.config import Config
from cpqs.core.catalog import Catalog, TableData
from cpqs.db.explain import ExplainResult
from cpqs.gen.data import set_based_insert_sql
from cpqs.gen.schema import create_table_sql, secondary_index_sql

TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "timeout exceeded",
    "query timeout",
    "execution timeout",
    "reply timeout",
    "max_execution_time",
    "max_statement_time",
    "statement timeout",
    "query execution was interrupted",
    "canceling statement",
    "cancelling statement",
    "call timeout",
    "dpi-1067",
    "dpy-4011",
    "ora-01013",
    "maximum statement execution time",
    "ob_query_timeout",
)
TIMEOUT_CODES = {3024, 1969}  # MySQL max_execution_time, MariaDB max_statement_time


CONNECTION_MARKERS = (
    "connection is closed",
    "interfaceerror",
    "server has gone away",
    "lost connection",
    "not connected",
    "(0, '')",
    "connection already closed",
)


def is_timeout_error(exc: BaseException) -> bool:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and args[0] in TIMEOUT_CODES:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in TIMEOUT_MARKERS)


def is_connection_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in CONNECTION_MARKERS)


class Adapter(ABC):
    name: str
    dialect: str

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.conn: Any = None

    @abstractmethod
    def connect(self) -> None:
        ...

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    def execute(self, sql: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            try:
                cur.fetchall()
            except Exception:
                pass
            if getattr(self.conn, "autocommit", True) in (False, 0):
                self.conn.commit()
        finally:
            cur.close()

    def fetchall(self, sql: str) -> List[Sequence[Any]]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            return list(rows)
        finally:
            cur.close()

    def setup_schema(self, catalog: Catalog) -> List[str]:
        stmts = self.prepare_database()
        # Drop MVs before base tables (Oracle dependency).
        for table in catalog.ordered_tables():
            if not table.mv_name:
                continue
            drop_mv = f"DROP MATERIALIZED VIEW {table.mv_name}"
            if self.dialect == "oracle":
                drop_mv = (
                    f"BEGIN EXECUTE IMMEDIATE 'DROP MATERIALIZED VIEW {table.mv_name}'; "
                    f"EXCEPTION WHEN OTHERS THEN NULL; END;"
                )
            stmts.append(drop_mv)
            try:
                self.execute(drop_mv)
            except Exception:
                self._rollback_quiet()
        # Drop children before parents (FK); create parents before children.
        # Disable FK checks for the MySQL family so a leftover ``t3`` from a
        # prior ``--tables 4`` run cannot block ``DROP t0``.
        fk_off = self.dialect in {
            "mysql",
            "mariadb",
            "tidb",
            "oceanbase",
        }
        if fk_off:
            try:
                self.execute("SET FOREIGN_KEY_CHECKS=0")
                stmts.append("SET FOREIGN_KEY_CHECKS=0")
            except Exception:
                self._rollback_quiet()
        # Also drop a small range of sibling names left by earlier runs.
        drop_names = [table.name for table in reversed(catalog.ordered_tables())]
        for i in range(8):
            name = f"t{i}"
            if name not in drop_names:
                drop_names.append(name)
        for name in drop_names:
            drop = self.drop_table_sql(name)
            stmts.append(drop)
            try:
                self.execute(drop)
            except Exception:
                self._rollback_quiet()
        if fk_off:
            try:
                self.execute("SET FOREIGN_KEY_CHECKS=1")
                stmts.append("SET FOREIGN_KEY_CHECKS=1")
            except Exception:
                self._rollback_quiet()
        for table in catalog.ordered_tables():
            create = create_table_sql(table, self.dialect)
            stmts.append(create)
            self.execute(create)
            for extra in table.extra_ddl:
                stmts.append(extra)
                self.execute(extra)
        return stmts

    def load_data(self, catalog: Catalog) -> List[str]:
        from cpqs.gen.data import insert_rows_sql

        stmts: List[str] = []
        self.before_bulk_load()
        for table in catalog.ordered_tables():
            insert = set_based_insert_sql(table, self.dialect, self._distinct_k(table))
            stmts.append(insert)
            try:
                self.execute(insert)
            except Exception:
                self._rollback_quiet()
                for batch in insert_rows_sql(table):
                    stmts.append(batch)
                    self.execute(batch)
            for idx in secondary_index_sql(table, self.dialect):
                stmts.append(idx)
                try:
                    self.execute(idx)
                except Exception:
                    self._rollback_quiet()
        for table in catalog.ordered_tables():
            for stmt in table.post_load_ddl:
                stmts.append(stmt)
                try:
                    self.execute(stmt)
                except Exception:
                    self._rollback_quiet()
        if self.cfg.analyze:
            stmts.extend(self.analyze_tables(catalog))
        return stmts

    def _distinct_k(self, table: TableData) -> int:
        return max(1, len(table.existing_values("k")))

    def prepare_database(self) -> List[str]:
        return []

    def before_bulk_load(self) -> None:
        return None

    def drop_table_sql(self, name: str) -> str:
        return f"DROP TABLE IF EXISTS {name}"

    def analyze_tables(self, catalog: Catalog) -> List[str]:
        stmts = [f"ANALYZE TABLE {name}" for name in catalog.tables]
        for stmt in stmts:
            try:
                self.execute(stmt)
            except Exception:
                self._rollback_quiet()
        return stmts

    def _rollback_quiet(self) -> None:
        try:
            self.conn.rollback()
        except Exception:
            pass

    @abstractmethod
    def explain(self, query: str) -> ExplainResult:
        ...

    def supports_explain_analyze(self) -> bool:
        return False

    def explain_analyze(self, query: str) -> ExplainResult:
        raise NotImplementedError(f"{self.name} does not support EXPLAIN ANALYZE")

    def _analyze_timeout_ms(self) -> int:
        return max(1000, int(self.cfg.timeout_s * 1000))

    def recover_session(self) -> None:
        try:
            self._rollback_quiet()
        except Exception:
            pass
        ping = getattr(self.conn, "ping", None)
        if callable(ping):
            try:
                ping(reconnect=True)
                return
            except TypeError:
                try:
                    ping()
                    return
                except Exception:
                    pass
            except Exception:
                pass
        try:
            self.close()
        except Exception:
            self.conn = None
        self.connect()
        try:
            self.prepare_database()
        except Exception:
            pass

    def timed_out_result(self, analyze_sql: str, exc: BaseException) -> ExplainResult:
        detail = f"ANALYZE timed out after {self.cfg.timeout_s}s: {exc}"
        return ExplainResult(
            analyze_sql,
            detail,
            None,
            None,
            analyze_sql=analyze_sql,
            analyze_text=detail,
            timed_out=True,
        )

    def supports_physical_lookup(self) -> bool:
        return False
