from __future__ import annotations

from typing import Any, List, Sequence

from cpqs.db.base import Adapter, is_timeout_error
from cpqs.db.explain import ExplainResult, parse_clickhouse_analyze, parse_clickhouse_estimate_rows


class ClickHouseAdapter(Adapter):
    name = "clickhouse"
    dialect = "clickhouse"

    def connect(self) -> None:
        import clickhouse_connect

        self._ch_kwargs = {
            "host": self.cfg.host,
            "port": self.cfg.port,
            "username": self.cfg.user,
            "password": self.cfg.password or "",
            "connect_timeout": max(5, int(self.cfg.timeout_s)),
            "send_receive_timeout": max(5, int(self.cfg.timeout_s)),
        }
        self.client = clickhouse_connect.get_client(database="default", **self._ch_kwargs)
        self.conn = self.client

    def close(self) -> None:
        client = getattr(self, "client", None)
        if client is not None:
            try:
                client.close()
            finally:
                self.client = None
                self.conn = None

    def execute(self, sql: str) -> None:
        self.client.command(sql)

    def fetchall(self, sql: str) -> List[Sequence[Any]]:
        result = self.client.query(sql)
        return [tuple(row) for row in result.result_rows]

    def prepare_database(self) -> List[str]:
        import clickhouse_connect

        db = self.cfg.database
        stmts = [f"CREATE DATABASE IF NOT EXISTS {db}"]
        self.execute(stmts[0])
        self.close()
        self.client = clickhouse_connect.get_client(database=db, **self._ch_kwargs)
        self.conn = self.client
        stmts.append(f"USE {db}")
        return stmts

    def analyze_tables(self, catalog) -> List[str]:
        del catalog
        return []

    def recover_session(self) -> None:
        try:
            self.close()
        except Exception:
            self.client = None
            self.conn = None
        self.connect()
        try:
            self.prepare_database()
        except Exception:
            pass

    def explain(self, query: str) -> ExplainResult:
        sql = "EXPLAIN ESTIMATE " + query
        try:
            rows = self.fetchall(sql)
            result = parse_clickhouse_estimate_rows(rows)
            result.sql = sql
            if result.max_rows is not None:
                return result
        except Exception:
            self.recover_session()
        sql = "EXPLAIN " + query
        rows = self.fetchall(sql)
        text = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)
        result = parse_clickhouse_analyze(text)
        result.sql = sql
        result.actual_max_rows = None
        result.actual_root_rows = None
        return result

    def supports_explain_analyze(self) -> bool:
        return True

    def explain_analyze(self, query: str) -> ExplainResult:
        timeout_s = max(1, int(self.cfg.timeout_s))
        try:
            self.execute(f"SET max_execution_time = {timeout_s}")
        except Exception:
            pass
        analyze_sql = "EXPLAIN ANALYZE " + query
        try:
            rows = self.fetchall(analyze_sql)
            text = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)
            result = parse_clickhouse_analyze(text)
            result.sql = analyze_sql
            result.analyze_sql = analyze_sql
            result.analyze_text = text
            if result.actual_max_rows is None:
                raise RuntimeError("ClickHouse EXPLAIN ANALYZE returned no row counts")
            return result
        except Exception as exc:
            if is_timeout_error(exc):
                self.recover_session()
                return self.timed_out_result(analyze_sql, exc)
            raise
        finally:
            try:
                self.execute("SET max_execution_time = 0")
            except Exception:
                pass
