from __future__ import annotations

import re
from typing import List

from cpqs.core.catalog import Catalog
from cpqs.db.base import Adapter, is_timeout_error
from cpqs.db.explain import ExplainResult, parse_oracle_xplan


def inject_oracle_gather_hint(query: str) -> str:
    existing = re.match(r"(?is)(\s*select\s*/\*\+\s*)(.*?)(\s*\*/)", query)
    if existing:
        return (
            f"{existing.group(1)}GATHER_PLAN_STATISTICS {existing.group(2)}"
            f"{existing.group(3)}{query[existing.end():]}"
        )
    hinted, n = re.subn(
        r"(?is)^\s*select\b",
        "SELECT /*+ GATHER_PLAN_STATISTICS */",
        query,
        count=1,
    )
    if n:
        return hinted
    hinted, n = re.subn(
        r"(?is)\bselect\b",
        "SELECT /*+ GATHER_PLAN_STATISTICS */",
        query,
        count=1,
    )
    return hinted if n else query


class OracleAdapter(Adapter):
    name = "oracle"
    dialect = "oracle"

    def connect(self) -> None:
        import oracledb

        dsn = f"{self.cfg.host}:{self.cfg.port}/{self.cfg.oracle_service}"
        self.conn = oracledb.connect(
            user=self.cfg.user,
            password=self.cfg.password,
            dsn=dsn,
        )
        self.conn.autocommit = True

    def drop_table_sql(self, name: str) -> str:
        return (
            "BEGIN "
            f"EXECUTE IMMEDIATE 'DROP TABLE {name} CASCADE CONSTRAINTS'; "
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; "
            "END;"
        )

    def prepare_database(self) -> List[str]:
        return []

    def analyze_tables(self, catalog: Catalog) -> List[str]:
        stmts = []
        for name in catalog.tables:
            stmt = (
                "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                f"ownname=>USER, tabname=>'{name.upper()}'); END;"
            )
            stmts.append(stmt)
            self.execute(stmt)
        return stmts

    def explain(self, query: str) -> ExplainResult:
        stmt_id = "cpqs1"
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                try:
                    self.fetchall("SELECT 1 FROM dual")
                except Exception:
                    self._rollback_quiet()
                self.execute(f"DELETE FROM plan_table WHERE statement_id = '{stmt_id}'")
                self.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{stmt_id}' FOR {query}")
                rows = self.fetchall(
                    f"SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, '{stmt_id}', 'TYPICAL'))"
                )
                text = "\n".join(str(r[0]) for r in rows)
                result = parse_oracle_xplan(text)
                result.sql = f"EXPLAIN PLAN SET STATEMENT_ID = '{stmt_id}' FOR {query}"
                return result
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "ORA-00900" not in msg and "DPY-4010" not in msg:
                    raise
                self._rollback_quiet()
        assert last_exc is not None
        raise last_exc
        rows = self.fetchall(
            f"SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, '{stmt_id}', 'TYPICAL'))"
        )
        text = "\n".join(str(r[0]) for r in rows)
        result = parse_oracle_xplan(text)
        result.sql = f"EXPLAIN PLAN SET STATEMENT_ID = '{stmt_id}' FOR {query}"
        return result

    def supports_explain_analyze(self) -> bool:
        return True

    def _display_cursor_allstats(self) -> str:
        rows = self.fetchall(
            "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))"
        )
        return "\n".join(str(r[0]) for r in rows)

    def explain_analyze(self, query: str) -> ExplainResult:
        timeout_ms = self._analyze_timeout_ms()
        prev_timeout = getattr(self.conn, "call_timeout", None)
        if hasattr(self.conn, "call_timeout"):
            self.conn.call_timeout = timeout_ms
        try:
            hinted = inject_oracle_gather_hint(query)
            self.fetchall(hinted)
            text = self._display_cursor_allstats()
            result = parse_oracle_xplan(text)
            if result.actual_max_rows is None:
                try:
                    self.execute("ALTER SESSION SET STATISTICS_LEVEL = ALL")
                    self.fetchall(query)
                    text = self._display_cursor_allstats()
                    result = parse_oracle_xplan(text)
                finally:
                    try:
                        self.execute("ALTER SESSION SET STATISTICS_LEVEL = TYPICAL")
                    except Exception:
                        pass
            result.sql = hinted
            result.analyze_sql = (
                hinted + ";\nSELECT PLAN_TABLE_OUTPUT FROM TABLE("
                "DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))"
            )
            result.analyze_text = text
            if result.actual_max_rows is None:
                raise RuntimeError("Oracle DISPLAY_CURSOR returned no A-Rows")
            return result
        except Exception as exc:
            if is_timeout_error(exc):
                self.recover_session()
                return self.timed_out_result(inject_oracle_gather_hint(query), exc)
            raise
        finally:
            if hasattr(self.conn, "call_timeout"):
                self.conn.call_timeout = prev_timeout if prev_timeout is not None else 0

    def supports_physical_lookup(self) -> bool:
        return True
