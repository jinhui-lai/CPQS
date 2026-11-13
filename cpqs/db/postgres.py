from __future__ import annotations

from typing import List

from cpqs.core.catalog import Catalog
from cpqs.db.base import Adapter, is_timeout_error
from cpqs.db.explain import ExplainResult, parse_cockroach_text, parse_postgres_text


class PostgresAdapter(Adapter):
    name = "postgres"
    dialect = "postgres"

    def connect(self) -> None:
        import psycopg

        self.conn = psycopg.connect(
            host=self.cfg.host,
            port=self.cfg.port,
            user=self.cfg.user,
            password=self.cfg.password,
            dbname="postgres",
            autocommit=True,
        )

    def prepare_database(self) -> List[str]:
        db = self.cfg.database
        exists = self.fetchall(f"SELECT 1 FROM pg_database WHERE datname = '{db}'")
        stmts: List[str] = []
        if not exists:
            stmt = f"CREATE DATABASE {db}"
            stmts.append(stmt)
            self.execute(stmt)
        self.conn.close()
        import psycopg

        self.conn = psycopg.connect(
            host=self.cfg.host,
            port=self.cfg.port,
            user=self.cfg.user,
            password=self.cfg.password,
            dbname=db,
            autocommit=True,
        )
        # CHECK-based emptiness proofs only apply with constraint_exclusion=on
        # (default is partition, which ignores CHECKs on ordinary tables).
        stmts.extend(self._enable_constraint_exclusion())
        return stmts

    def _enable_constraint_exclusion(self) -> List[str]:
        stmt = "SET constraint_exclusion = on"
        try:
            self.execute(stmt)
            return [stmt]
        except Exception:
            self._rollback_quiet()
            return []

    def recover_session(self) -> None:
        super().recover_session()
        if self.name in {"postgres", "opengauss"}:
            self._enable_constraint_exclusion()

    def analyze_tables(self, catalog: Catalog) -> List[str]:
        stmts = [f"ANALYZE {name}" for name in catalog.tables]
        for stmt in stmts:
            self.execute(stmt)
        return stmts

    def explain(self, query: str) -> ExplainResult:
        rows = self.fetchall("EXPLAIN " + query)
        text = "\n".join(str(r[0]) for r in rows)
        result = parse_postgres_text(text)
        result.sql = "EXPLAIN " + query
        return result

    def supports_explain_analyze(self) -> bool:
        return True

    def explain_analyze(self, query: str) -> ExplainResult:
        timeout_ms = self._analyze_timeout_ms()
        try:
            self.execute(f"SET statement_timeout = {timeout_ms}")
        except Exception:
            pass
        analyze_sql = "EXPLAIN (ANALYZE, TIMING ON) " + query
        try:
            rows = self.fetchall(analyze_sql)
            text = "\n".join(str(r[0]) for r in rows)
            result = parse_postgres_text(text)
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
                self.execute("SET statement_timeout = 0")
            except Exception:
                pass

    def supports_physical_lookup(self) -> bool:
        return True


class CockroachAdapter(PostgresAdapter):
    name = "cockroach"
    dialect = "cockroach"

    def _connect_to(self, dbname: str) -> None:
        import psycopg

        self.conn = psycopg.connect(
            conninfo=(
                f"host={self.cfg.host} port={self.cfg.port} user={self.cfg.user} "
                f"dbname={dbname} sslmode=disable"
            ),
            autocommit=True,
        )

    def connect(self) -> None:
        self._connect_to("defaultdb")

    def prepare_database(self) -> List[str]:
        db = self.cfg.database
        stmts = [f"CREATE DATABASE IF NOT EXISTS {db}"]
        self.execute(stmts[0])
        self.conn.close()
        self._connect_to(db)
        return stmts

    def explain(self, query: str) -> ExplainResult:
        rows = self.fetchall("EXPLAIN (VERBOSE) " + query)
        text = "\n".join("" if r[0] is None else str(r[0]) for r in rows)
        result = parse_cockroach_text(text)
        result.sql = "EXPLAIN (VERBOSE) " + query
        return result

    def supports_explain_analyze(self) -> bool:
        return True

    def explain_analyze(self, query: str) -> ExplainResult:
        timeout_ms = self._analyze_timeout_ms()
        try:
            self.execute(f"SET statement_timeout = '{timeout_ms}ms'")
        except Exception:
            pass
        analyze_sql = "EXPLAIN ANALYZE (VERBOSE) " + query
        try:
            rows = self.fetchall(analyze_sql)
            text = "\n".join("" if r[0] is None else str(r[0]) for r in rows)
            result = parse_cockroach_text(text)
            result.sql = analyze_sql
            result.analyze_sql = analyze_sql
            result.analyze_text = text
            if result.actual_max_rows is None:
                raise RuntimeError("Cockroach EXPLAIN ANALYZE returned no actual row count")
            return result
        except Exception as exc:
            if is_timeout_error(exc):
                self.recover_session()
                return self.timed_out_result(analyze_sql, exc)
            raise
        finally:
            try:
                self.execute("SET statement_timeout = '0s'")
            except Exception:
                pass

    def supports_physical_lookup(self) -> bool:
        return False


class OpenGaussAdapter(PostgresAdapter):
    name = "opengauss"
    dialect = "opengauss"
