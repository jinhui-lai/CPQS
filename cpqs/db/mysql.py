from __future__ import annotations

import json
from typing import List

from cpqs.db.base import Adapter, is_timeout_error
from cpqs.db.explain import ExplainResult, parse_mysql_json, parse_mysql_tree, parse_tabular_rows


class MySQLAdapter(Adapter):
    name = "mysql"
    dialect = "mysql"

    def _revive(self) -> None:
        """Reconnect and re-enter the target database.

        ``ping(reconnect=True)`` drops the session ``USE`` and would make the
        next statement fail with 'No database selected'.
        """
        if not self.conn:
            return
        try:
            self.conn.ping(reconnect=True)
        except Exception:
            return
        try:
            self.execute(f"USE {self.cfg.database}")
        except Exception:
            pass

    def connect(self) -> None:
        import pymysql

        self.conn = pymysql.connect(
            host=self.cfg.host,
            port=self.cfg.port,
            user=self.cfg.user,
            password=self.cfg.password,
            autocommit=True,
            charset="utf8mb4",
        )

    def prepare_database(self) -> List[str]:
        stmts = [
            f"CREATE DATABASE IF NOT EXISTS {self.cfg.database}",
            f"USE {self.cfg.database}",
        ]
        self.execute(stmts[0])
        self.execute(stmts[1])
        return stmts

    def before_bulk_load(self) -> None:
        depth = max(self.cfg.rows, self.cfg.rows_small) + 10
        self.execute(f"SET SESSION cte_max_recursion_depth = {depth}")

    def explain(self, query: str) -> ExplainResult:
        rows = self.fetchall("EXPLAIN FORMAT=TREE " + query)
        text = "\n".join(str(r[0]) for r in rows if r and r[0] is not None)
        result = parse_mysql_tree(text)
        result.sql = "EXPLAIN FORMAT=TREE " + query
        if result.max_rows is not None:
            return result
        try:
            rows = self.fetchall("EXPLAIN FORMAT=JSON " + query)
            if rows and rows[0][0]:
                parsed = parse_mysql_json(rows[0][0])
                if parsed is not None:
                    parsed.sql = "EXPLAIN FORMAT=JSON " + query
                    return parsed
        except Exception:
            self._revive()
        return result

    def supports_explain_analyze(self) -> bool:
        return True

    def explain_analyze(self, query: str) -> ExplainResult:
        timeout_ms = self._analyze_timeout_ms()
        try:
            self.execute(f"SET SESSION max_execution_time = {timeout_ms}")
        except Exception:
            pass
        analyze_sql = "EXPLAIN ANALYZE FORMAT=TREE " + query
        try:
            rows = self.fetchall(analyze_sql)
            text = "\n".join(str(r[0]) for r in rows if r and r[0] is not None)
            result = parse_mysql_tree(text)
            result.sql = analyze_sql
            result.analyze_sql = analyze_sql
            result.analyze_text = text
            return result
        except Exception as exc:
            if is_timeout_error(exc):
                self.recover_session()
                return self.timed_out_result(analyze_sql, exc)
            raise
        finally:
            try:
                self.execute("SET SESSION max_execution_time = 0")
            except Exception:
                pass


class MariaDBAdapter(MySQLAdapter):
    name = "mariadb"
    dialect = "mariadb"

    def before_bulk_load(self) -> None:
        depth = max(self.cfg.rows, self.cfg.rows_small) + 10
        try:
            self.execute(f"SET SESSION max_recursive_iterations = {depth}")
        except Exception:
            pass

    def explain(self, query: str) -> ExplainResult:
        try:
            rows = self.fetchall("EXPLAIN FORMAT=JSON " + query)
            if rows and rows[0][0]:
                raw = rows[0][0]
                text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
                parsed = parse_mysql_json(text)
                if parsed is not None:
                    parsed.sql = "EXPLAIN FORMAT=JSON " + query
                    return parsed
        except Exception:
            pass
        rows = self.fetchall("EXPLAIN " + query)
        # tabular: id, select_type, table, type, possible_keys, key, key_len, ref, rows, Extra
        result = parse_tabular_rows(rows, row_index=8, id_index=0)
        result.sql = "EXPLAIN " + query
        return result

    def explain_analyze(self, query: str) -> ExplainResult:
        timeout_s = max(1, int(self.cfg.timeout_s))
        try:
            self.execute(f"SET SESSION max_statement_time = {timeout_s}")
        except Exception:
            pass
        try:
            for prefix in ("ANALYZE FORMAT=JSON ", "EXPLAIN ANALYZE FORMAT=JSON ", "EXPLAIN ANALYZE "):
                try:
                    rows = self.fetchall(prefix + query)
                except Exception as exc:
                    if is_timeout_error(exc):
                        self.recover_session()
                        return self.timed_out_result(prefix + query, exc)
                    self._revive()
                    continue
                if not rows:
                    continue
                raw = rows[0][0]
                if raw is None and len(rows[0]) > 1:
                    raw = rows[0][1]
                if isinstance(raw, (dict, list)) or (isinstance(raw, str) and raw.lstrip().startswith("{")):
                    text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
                    parsed = parse_mysql_json(text)
                    if parsed is not None:
                        parsed.sql = prefix + query
                        parsed.analyze_sql = parsed.sql
                        parsed.analyze_text = text
                        return parsed
                text = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)
                parsed = parse_mysql_tree(text)
                parsed.sql = prefix + query
                parsed.analyze_sql = parsed.sql
                parsed.analyze_text = text
                if parsed.actual_max_rows is not None or parsed.max_rows is not None:
                    return parsed
            raise RuntimeError("MariaDB ANALYZE returned no row counts")
        finally:
            try:
                self.execute("SET SESSION max_statement_time = 0")
            except Exception:
                pass


class TiDBAdapter(MySQLAdapter):
    name = "tidb"
    dialect = "tidb"

    def before_bulk_load(self) -> None:
        depth = max(self.cfg.rows, self.cfg.rows_small) + 10
        try:
            self.execute(f"SET SESSION cte_max_recursion_depth = {depth}")
        except Exception:
            pass

    def explain(self, query: str) -> ExplainResult:
        rows = self.fetchall("EXPLAIN " + query)
        # id, estRows, task, access object, operator info
        result = parse_tabular_rows(rows, row_index=1, id_index=0)
        result.sql = "EXPLAIN " + query
        return result

    def supports_explain_analyze(self) -> bool:
        return True

    def explain_analyze(self, query: str) -> ExplainResult:
        timeout_ms = self._analyze_timeout_ms()
        try:
            self.execute(f"SET SESSION max_execution_time = {timeout_ms}")
        except Exception:
            pass
        try:
            analyze_sql = "EXPLAIN ANALYZE " + query
            rows = self.fetchall(analyze_sql)
            result = parse_tabular_rows(rows, row_index=1, id_index=0, actual_row_index=2)
            result.sql = analyze_sql
            result.analyze_sql = analyze_sql
            result.analyze_text = result.text
            if result.actual_max_rows is None:
                raise RuntimeError("TiDB EXPLAIN ANALYZE returned no actRows")
            return result
        except Exception as exc:
            if is_timeout_error(exc):
                self.recover_session()
                return self.timed_out_result("EXPLAIN ANALYZE " + query, exc)
            raise
        finally:
            try:
                self.execute("SET SESSION max_execution_time = 0")
            except Exception:
                pass


class OceanBaseAdapter(MySQLAdapter):
    name = "oceanbase"
    dialect = "oceanbase"

    def before_bulk_load(self) -> None:
        depth = max(self.cfg.rows, self.cfg.rows_small) + 10
        try:
            self.execute(f"SET SESSION cte_max_recursion_depth = {depth}")
        except Exception:
            pass
        try:
            self.execute(f"SET SESSION ob_query_timeout = {self._analyze_timeout_ms() * 1000}")
        except Exception:
            pass

    def explain(self, query: str) -> ExplainResult:
        from cpqs.db.explain import parse_oceanbase_plan

        rows = self.fetchall("EXPLAIN " + query)
        text = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)
        result = parse_oceanbase_plan(text)
        result.sql = "EXPLAIN " + query
        if result.max_rows is not None:
            return result
        result = parse_tabular_rows(rows, row_index=3, id_index=0)
        result.sql = "EXPLAIN " + query
        return result

    def explain_analyze(self, query: str) -> ExplainResult:
        from cpqs.db.explain import parse_oceanbase_plan

        timeout_us = max(1_000_000, int(self.cfg.timeout_s * 1_000_000))
        try:
            self.execute(f"SET SESSION ob_query_timeout = {timeout_us}")
        except Exception:
            pass
        try:
            self.execute(f"SET SESSION max_execution_time = {self._analyze_timeout_ms()}")
        except Exception:
            pass
        last_exc: Exception | None = None
        last_plan: ExplainResult | None = None
        for prefix in ("EXPLAIN ANALYZE ", "EXPLAIN FORMAT=JSON "):
            analyze_sql = prefix + query
            try:
                rows = self.fetchall(analyze_sql)
            except Exception as exc:
                if is_timeout_error(exc):
                    self.recover_session()
                    return self.timed_out_result(analyze_sql, exc)
                last_exc = exc
                self._revive()
                continue
            text = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)
            raw0 = rows[0][0] if rows and rows[0] else None
            if isinstance(raw0, (dict, list)) or (isinstance(raw0, str) and raw0.lstrip().startswith("{")):
                parsed = parse_mysql_json(raw0 if isinstance(raw0, str) else json.dumps(raw0, default=str))
                if parsed is not None:
                    parsed.sql = analyze_sql
                    parsed.analyze_sql = analyze_sql
                    parsed.analyze_text = parsed.text
                    last_plan = parsed
                    if parsed.actual_max_rows is not None:
                        return parsed
                    continue
            parsed = parse_oceanbase_plan(text)
            parsed.sql = analyze_sql
            parsed.analyze_sql = analyze_sql
            parsed.analyze_text = text
            last_plan = parsed
            if parsed.actual_max_rows is not None:
                return parsed
        analyze_sql = "SHOW TRACE"
        try:
            self.execute("SET SESSION ob_enable_show_trace = 1")
        except Exception:
            try:
                self.execute("SET ob_enable_trace = 1")
            except Exception:
                pass
        try:
            self.fetchall(query)
            rows = self.fetchall("SHOW TRACE")
            text = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)
            parsed = parse_oceanbase_plan(text)
            parsed.sql = analyze_sql
            parsed.analyze_sql = analyze_sql
            parsed.analyze_text = text
            if parsed.actual_max_rows is None and parsed.max_rows is not None:
                parsed.actual_max_rows = parsed.max_rows
                parsed.actual_root_rows = parsed.root_rows
            if parsed.actual_max_rows is None and last_plan is not None and last_plan.actual_max_rows is not None:
                return last_plan
            if parsed.actual_max_rows is None:
                raise RuntimeError("OceanBase ANALYZE returned no row counts")
            return parsed
        except Exception as exc:
            if is_timeout_error(exc):
                self.recover_session()
                return self.timed_out_result(analyze_sql, exc)
            if last_plan is not None and last_plan.actual_max_rows is not None:
                return last_plan
            if last_exc is not None:
                raise last_exc
            raise
        finally:
            try:
                self.execute("SET SESSION max_execution_time = 0")
            except Exception:
                pass
