from __future__ import annotations

from pathlib import Path
from typing import List

from cpqs.config import Config
from cpqs.core.query import QuerySpec
from cpqs.db.explain import ExplainResult
from cpqs.gen.data import set_based_insert_sql
from cpqs.gen.schema import create_table_sql, secondary_index_sql
from cpqs.core.catalog import Catalog


def write_poc(
    cfg: Config,
    catalog: Catalog,
    spec: QuerySpec,
    query_sql: str,
    plan: ExplainResult,
    bug_id: int,
    executed: ExplainResult | None = None,
) -> Path:
    out_dir = Path(cfg.poc_dir) / cfg.db
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"bug-{bug_id:04d}-{spec.path_kind}.sql"
    lines: List[str] = [
        f"-- CPQS performance / cardinality bug #{bug_id}",
        f"-- schema profile: {catalog.profile_name}",
        f"-- access path: {spec.path_kind}",
        f"-- {spec.comment}",
        f"-- expected access cardinality: {spec.oracle.describe()}",
        f"-- explained max rows: {plan.access_rows}",
        f"-- explained root rows: {plan.root_rows}",
    ]
    if executed is not None:
        if executed.timed_out:
            lines.append(f"-- analyzed: TIMED OUT after {cfg.timeout_s}s (counts as bug)")
        else:
            lines.extend(
                [
                    f"-- analyzed actual max rows: {executed.actual_access_rows}",
                    f"-- analyzed actual root rows: {executed.actual_root_rows}",
                ]
            )
    for note in spec.notes:
        lines.append(f"-- note: {note}")
    lines.append("")
    depth = catalog.max_n() + 10
    if cfg.db in {"mysql", "tidb", "oceanbase"}:
        lines.append(f"SET SESSION cte_max_recursion_depth = {depth};")
        lines.append("")
    elif cfg.db == "mariadb":
        lines.append(f"SET SESSION max_recursive_iterations = {depth};")
        lines.append("")
    elif cfg.db in {"postgres", "opengauss"}:
        # CHECK emptiness proofs only apply with exclusion=on (session default
        # is partition, which ignores CHECKs on ordinary tables).
        lines.append("SET constraint_exclusion = on;")
        lines.append("")
    elif cfg.db == "oracle":
        lines.append("-- Oracle uses CHECK for plan exclusion without a GUC")
        lines.append("")
    for table in catalog.tables.values():
        lines.append(f"DROP TABLE IF EXISTS {table.name};")
        lines.append(create_table_sql(table, cfg.db) + ";")
        for extra in table.extra_ddl:
            lines.append(extra + ";")
        distinct = max(1, len(table.existing_values("k")))
        lines.append(set_based_insert_sql(table, cfg.db, distinct) + ";")
        for idx in secondary_index_sql(table, cfg.db):
            lines.append(idx + ";")
        lines.append("")
    lines.append(query_sql + ";")
    lines.append("")
    lines.append(f"-- {plan.sql}")
    lines.append("/*")
    lines.append(plan.text)
    lines.append("*/")
    if executed is not None and (executed.analyze_text or executed.text):
        lines.append("")
        lines.append(f"-- {executed.analyze_sql or executed.sql}")
        lines.append("/*")
        lines.append(executed.analyze_text or executed.text)
        lines.append("*/")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
