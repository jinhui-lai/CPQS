"""Parse CPQS POC files and shrink them while the bug still reproduces.

Reductions are applied greedily and each candidate is checked live with the
same dual-validation rule as the fuzzer (EXPLAIN overestimate, then ANALYZE
or timeout). Unused tables are the only offline-safe edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple

from cpqs.config import Config
from cpqs.core.oracle import CardinalityOracle
from cpqs.core.query import QuerySpec
from cpqs.db.base import Adapter, is_connection_error, is_timeout_error
from cpqs.db.explain import ExplainResult
from cpqs.runner import is_violation


KNOWN_COLS = ("c0", "c1", "k", "a", "b", "u0", "u1", "part", "payload")
TABLE_RE = re.compile(r"\bt([0-3])\b", re.I)
COL_RE = re.compile(r"\b(c0|c1|k|a|b|u0|u1|part|payload)\b", re.I)
ORACLE_RE = re.compile(
    r"expected access cardinality:\s*\[(\d+),\s*(\d+)\]",
    re.I,
)
PATH_RE = re.compile(r"access path:\s*(\S+)", re.I)
TAUTOLOGY_RE = re.compile(
    r"(?i)\s+AND\s+(?:TRUE|1\s*=\s*1|\(\s*TRUE\s*\)|\(\s*1\s*=\s*1\s*\))\s*"
)
LEADING_TAUTOLOGY_RE = re.compile(
    r"(?i)^\s*(?:TRUE|1\s*=\s*1)\s+AND\s+"
)


@dataclass
class PocFile:
    path: Path
    header: List[str]
    setup: List[str]
    query: str
    oracle: CardinalityOracle
    path_kind: str
    had_analyze: bool
    timed_out: bool
    notes: List[str] = field(default_factory=list)

    def clone(self) -> "PocFile":
        return replace(
            self,
            header=list(self.header),
            setup=list(self.setup),
            notes=list(self.notes),
        )


def split_top_level(text: str, sep: str = ",") -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            elif ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 1
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and text.startswith(sep, i):
            parts.append("".join(buf).strip())
            buf = []
            i += len(sep)
            continue
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def split_statements(sql: str) -> List[str]:
    stmts: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = ""
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == ";" and depth == 0:
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _strip_explain_tail(text: str) -> str:
    keep: List[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if (
            stripped.startswith("-- EXPLAIN")
            or stripped.startswith("-- PLAN ")
            or stripped.startswith("-- TRACE")
            or stripped.startswith("-- SELECT /*+")
        ):
            break
        keep.append(line)
    return "\n".join(keep)


def _strip_leading_sql_comments(stmt: str) -> str:
    """Drop leading -- / /* */ so annotated good/bad demos still yield the SELECT."""
    s = stmt
    while True:
        s2 = s.lstrip()
        if s2.startswith("--"):
            nl = s2.find("\n")
            if nl < 0:
                return ""
            s = s2[nl + 1 :]
            continue
        if s2.startswith("/*"):
            end = s2.find("*/")
            if end < 0:
                return ""
            s = s2[end + 2 :]
            continue
        return s2


def _is_poc_query(stmt: str) -> bool:
    s = _strip_leading_sql_comments(stmt)
    if not re.match(r"(?is)^select\b", s):
        return False
    # Oracle plan dump companion, not the cardinality query.
    if re.search(r"(?is)\bdbms_xplan\b", s):
        return False
    return True


def parse_poc(path: Path) -> PocFile:
    raw = path.read_text(encoding="utf-8", errors="replace")
    header: List[str] = []
    body_lines: List[str] = []
    in_header = True
    had_analyze = False
    timed_out = False
    oracle = CardinalityOracle.exact_count(1)
    stem_kind = re.sub(r"(?i)^bug-\d+-", "", path.stem)
    path_kind = stem_kind or path.stem
    # Empty-proof kinds default to [0,0] when the hand-written header omits it.
    if path_kind in {
        "partition_out_of_range",
        "partition_bound_hole",
        "partition_expr_empty",
        "check_expr_wrap",
        "check_pair_arith",
        "check_or_join_empty",
        "check_domain_baseline",
        "check_pair_point",
    }:
        oracle = CardinalityOracle.exact_count(0)
    for line in raw.splitlines():
        if in_header and line.startswith("--"):
            header.append(line)
            m = ORACLE_RE.search(line)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                oracle = CardinalityOracle(lo, hi, lo == hi)
            m = PATH_RE.search(line)
            if m:
                path_kind = m.group(1)
            if "analyzed" in line.lower():
                had_analyze = True
                if "timed out" in line.lower():
                    timed_out = True
            continue
        in_header = False
        body_lines.append(line)
    # Prefer stripping trailing explain dumps only after a real POC SELECT so
    # hand-written good/bad -- EXPLAIN demos before the query stay intact.
    body = "\n".join(body_lines)
    stmts_raw = [s for s in split_statements(body) if s.strip()]
    query_idx = None
    for i, stmt in enumerate(stmts_raw):
        if _is_poc_query(stmt):
            query_idx = i
    if query_idx is None:
        # Fallback: fuzzer POCs with only trailing -- EXPLAIN after SELECT.
        body = _strip_explain_tail(body)
        stmts_raw = [s for s in split_statements(body) if s.strip()]
        for i, stmt in enumerate(stmts_raw):
            if _is_poc_query(stmt):
                query_idx = i
    if query_idx is None:
        raise ValueError(f"no SELECT query in {path}")
    setup: List[str] = []
    for stmt in stmts_raw[:query_idx]:
        cleaned = _strip_leading_sql_comments(stmt).rstrip(";").strip()
        if not cleaned:
            continue
        if _is_poc_query(stmt) or re.match(r"(?is)^select\b", cleaned):
            # Skip earlier demo SELECTs / XPLAN companions in setup.
            continue
        # Skip hand-written good-case EXPLAIN demos (not setup DDL/DML).
        if re.match(r"(?is)^explain\b", cleaned):
            continue
        setup.append(cleaned)
    query = _strip_leading_sql_comments(stmts_raw[query_idx]).rstrip(";").strip()
    return PocFile(
        path=path,
        header=header,
        setup=setup,
        query=query,
        oracle=oracle,
        path_kind=path_kind,
        had_analyze=had_analyze,
        timed_out=timed_out,
    )


def referenced_tables(sql: str) -> Set[str]:
    return {m.group(0).lower() for m in TABLE_RE.finditer(sql)}


def referenced_columns(sql: str) -> Set[str]:
    return {m.group(1).lower() for m in COL_RE.finditer(sql)}


def _table_of_setup(stmt: str) -> Optional[str]:
    m = re.search(
        r"(?is)^(?:drop\s+table(?:\s+if\s+exists)?|create\s+table(?:\s+if\s+not\s+exists)?|insert\s+into)\s+(t[0-3])(?:_p\d+)?\b",
        stmt,
    )
    if m:
        return m.group(1).lower()
    m = re.search(r"(?is)^create\s+(?:unique\s+)?index\s+\S+\s+on\s+(t[0-3])\b", stmt)
    if m:
        return m.group(1).lower()
    return None


def drop_unused_tables(poc: PocFile) -> Optional[PocFile]:
    used = referenced_tables(poc.query)
    if not used:
        return None
    setup = [s for s in poc.setup if (_table_of_setup(s) or "") in used or _table_of_setup(s) is None]
    if setup == poc.setup:
        return None
    out = poc.clone()
    out.setup = setup
    out.notes.append("dropped unused tables")
    return out


def is_index_stmt(stmt: str) -> bool:
    return bool(re.match(r"(?is)^create\s+(?:unique\s+)?index\b", stmt.strip()))


def index_key_columns(stmt: str) -> Set[str]:
    m = re.search(
        r"(?is)^create\s+(?:unique\s+)?index\s+(?:\S+\s+)?on\s+t[0-3]\s*\(([^)]*)\)",
        stmt.strip(),
    )
    if not m:
        return set()
    keys: Set[str] = set()
    for part in split_top_level(m.group(1)):
        ident = re.sub(r"(?is)^(asc|desc)\s+", "", part.strip())
        ident = ident.split()[0] if ident else ""
        ident = ident.strip("`\"[]")
        if ident.lower() in KNOWN_COLS:
            keys.add(ident.lower())
    return keys


def drop_one_index(poc: PocFile, idx: int) -> Optional[PocFile]:
    indexes = [i for i, s in enumerate(poc.setup) if is_index_stmt(s)]
    if idx >= len(indexes):
        return None
    pos = indexes[idx]
    stmt = poc.setup[pos]
    if index_key_columns(stmt) & referenced_columns(poc.query):
        return None
    out = poc.clone()
    out.setup = poc.setup[:pos] + poc.setup[pos + 1 :]
    out.notes.append(f"dropped index: {stmt[:80]}")
    return out


def _create_paren_span(stmt: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"(?is)^create\s+table\b", stmt)
    if not m:
        return None
    start = stmt.find("(", m.end())
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(stmt)):
        if stmt[i] == "(":
            depth += 1
        elif stmt[i] == ")":
            depth -= 1
            if depth == 0:
                return start, i
    return None


def _ddl_guard_columns(stmt: str) -> Set[str]:
    guards: Set[str] = set()
    for m in re.finditer(
        r"(?is)(?:primary\s+key|unique(?:\s+index)?|order\s+by|partition\s+by|partitioned\s+by)\s*\(([^)]*)\)",
        stmt,
    ):
        for part in split_top_level(m.group(1)):
            ident = re.sub(r"(?is)^(asc|desc)\s+", "", part.strip())
            ident = ident.split()[0] if ident else ""
            ident = ident.strip("`\"[]")
            if ident.lower() in KNOWN_COLS:
                guards.add(ident.lower())
    for m in re.finditer(r"(?is)order\s+by\s+(t[0-3]\.)?([A-Za-z0-9_]+)", stmt):
        if m.group(2).lower() in KNOWN_COLS:
            guards.add(m.group(2).lower())
    return guards


def _piece_column_name(piece: str) -> Optional[str]:
    raw = piece.strip()
    if re.match(r"(?is)^(primary\s+key|unique|key|constraint|index|check|foreign)\b", raw):
        return None
    name = raw.split()[0].strip("`\"[]")
    return name.lower() if name.lower() in KNOWN_COLS else name.lower()


def _insert_select_span(stmt: str) -> Optional[Tuple[List[str], int, int]]:
    m = re.search(r"(?is)^insert\s+into\s+t[0-3]\s*\(([^)]*)\)\s*", stmt)
    if not m:
        return None
    cols = [c.strip().strip("`\"[]").lower() for c in split_top_level(m.group(1))]
    rest = stmt[m.end() :]
    depth = 0
    quote = ""
    select_at = None
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and rest[i : i + 6].lower() == "select" and (i == 0 or not rest[i - 1].isalnum()):
            select_at = i
        i += 1
    if select_at is None:
        return None
    sel = rest[select_at + 6 :]
    depth = 0
    quote = ""
    from_at = None
    i = 0
    while i < len(sel):
        ch = sel[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and sel[i : i + 4].lower() == "from" and (i == 0 or not sel[i - 1].isalnum()):
            from_at = i
            break
        i += 1
    if from_at is None:
        return None
    abs_sel = m.end() + select_at + 6
    return cols, abs_sel, abs_sel + from_at


def _piece_refs_column(piece: str, column: str) -> bool:
    return bool(re.search(rf"(?is)\b{re.escape(column)}\b", piece))


def _is_primary_key_piece(piece: str) -> bool:
    raw = piece.strip()
    return bool(
        re.match(r"(?is)^primary\s+key\b", raw)
        or re.match(r"(?is)^constraint\s+\w+\s+primary\s+key\b", raw)
    )


def _is_droppable_constraint_piece(piece: str) -> bool:
    """Table-level UNIQUE/KEY/CHECK (not PRIMARY KEY) — safe to strip with a column."""
    raw = piece.strip()
    if _is_primary_key_piece(raw):
        return False
    return bool(
        re.match(
            r"(?is)^(unique(?:\s+index)?|key|index|check|constraint)\b",
            raw,
        )
    )


def drop_column(poc: PocFile, column: str) -> Optional[PocFile]:
    column = column.lower()
    if column in referenced_columns(poc.query):
        return None
    if re.search(r"(?is)\bnatural\b", poc.query):
        return None
    changed = False
    new_setup: List[str] = []
    for stmt in poc.setup:
        if re.match(r"(?is)^create\s+table\b", stmt):
            pk = bool(re.search(rf"(?is)primary\s+key\s*\([^)]*\b{re.escape(column)}\b", stmt))
            order = bool(re.search(rf"(?is)order\s+by\s*\([^)]*\b{re.escape(column)}\b", stmt))
            part_by = bool(
                re.search(
                    rf"(?is)partition(?:ed)?\s+by\s*\([^)]*\b{re.escape(column)}\b",
                    stmt,
                )
            )
            if pk or order or part_by:
                # CREATE still needs the column; do not drop it from INSERT alone.
                return None
        span = _create_paren_span(stmt)
        if span:
            inner = stmt[span[0] + 1 : span[1]]
            pieces = split_top_level(inner)
            kept = []
            removed = False
            for piece in pieces:
                name = _piece_column_name(piece)
                if name == column:
                    removed = True
                    continue
                # Drop UNIQUE/KEY/CHECK that reference the removed column so DDL
                # stays valid (e.g. UNIQUE (c1, part) or CHECK (k <= c0)).
                if name is None and _is_droppable_constraint_piece(piece) and _piece_refs_column(
                    piece, column
                ):
                    removed = True
                    continue
                kept.append(piece)
            if removed and kept:
                stmt = stmt[: span[0] + 1] + ", ".join(kept) + stmt[span[1] :]
                changed = True
            new_setup.append(stmt)
            continue
        ins = _insert_select_span(stmt)
        if ins:
            cols, sel_lo, sel_hi = ins
            if column not in cols:
                new_setup.append(stmt)
                continue
            exprs = split_top_level(stmt[sel_lo:sel_hi])
            if len(exprs) != len(cols):
                new_setup.append(stmt)
                continue
            pairs = [(c, e) for c, e in zip(cols, exprs) if c != column]
            if not pairs:
                new_setup.append(stmt)
                continue
            new_cols = ", ".join(c for c, _ in pairs)
            new_exprs = ", ".join(e.strip() for _, e in pairs)
            prefix = re.sub(
                r"(?is)^(insert\s+into\s+t[0-3]\s*)\([^)]*\)",
                rf"\1({new_cols})",
                stmt[:sel_lo],
                count=1,
            )
            stmt = prefix + " " + new_exprs + " " + stmt[sel_hi:]
            changed = True
            new_setup.append(stmt)
            continue
        if is_index_stmt(stmt) and re.search(rf"(?is)\b{re.escape(column)}\b", stmt):
            changed = True
            continue
        new_setup.append(stmt)
    if not changed:
        return None
    out = poc.clone()
    out.setup = new_setup
    out.notes.append(f"dropped column {column}")
    return out


def strip_tautologies(poc: PocFile) -> Optional[PocFile]:
    q = TAUTOLOGY_RE.sub(" ", poc.query)
    q = LEADING_TAUTOLOGY_RE.sub("", q)
    q = re.sub(r"\s+", " ", q).strip()
    if q == poc.query:
        return None
    out = poc.clone()
    out.query = q
    out.notes.append("stripped tautology predicates")
    return out


def _clause_span(sql: str, keyword: str) -> Optional[Tuple[int, int]]:
    m = re.search(rf"(?is)\b{keyword}\b", sql)
    if not m:
        return None
    start = m.end()
    stop_at = re.search(
        r"(?is)\b(group\s+by|having|order\s+by|limit|offset|fetch|window)\b",
        sql[start:],
    )
    end = start + stop_at.start() if stop_at else len(sql)
    return start, end


def drop_and_conjunct(poc: PocFile, keyword: str, which: int) -> Optional[PocFile]:
    span = _clause_span(poc.query, keyword)
    if not span:
        return None
    body = poc.query[span[0] : span[1]].strip()
    parts = split_top_level(body, sep=" AND ")
    if len(parts) < 2 or which >= len(parts):
        return None
    kept = [p for i, p in enumerate(parts) if i != which]
    new_body = " AND ".join(kept)
    out = poc.clone()
    out.query = (poc.query[: span[0]] + " " + new_body + " " + poc.query[span[1] :]).strip()
    out.query = re.sub(r"\s+", " ", out.query)
    out.notes.append(f"dropped {keyword} conjunct: {parts[which][:60]}")
    return out


def narrow_select_star(poc: PocFile) -> Optional[PocFile]:
    if not re.match(r"(?is)^select\s+\*\s+from\b", poc.query):
        return None
    cols = [c for c in KNOWN_COLS if c in referenced_columns(poc.query)]
    if not cols or len(referenced_tables(poc.query)) > 1:
        return None
    listed = ", ".join(cols)
    out = poc.clone()
    out.query = re.sub(r"(?is)^select\s+\*\s+", f"SELECT {listed} ", poc.query, count=1)
    out.notes.append(f"replaced SELECT * with {listed}")
    return out


def drop_order_term(poc: PocFile, which_from_end: int) -> Optional[PocFile]:
    span = _clause_span(poc.query, "order by")
    if not span:
        return None
    body = poc.query[span[0] : span[1]].strip()
    parts = split_top_level(body)
    if len(parts) < 2:
        return None
    idx = len(parts) - 1 - which_from_end
    if idx < 0:
        return None
    kept = [p for i, p in enumerate(parts) if i != idx]
    out = poc.clone()
    out.query = poc.query[: span[0]] + " " + ", ".join(kept) + " " + poc.query[span[1] :]
    out.query = re.sub(r"\s+", " ", out.query).strip()
    out.notes.append(f"dropped ORDER BY term: {parts[idx]}")
    return out


def drop_set_stmt(poc: PocFile, idx: int) -> Optional[PocFile]:
    sets = [i for i, s in enumerate(poc.setup) if re.match(r"(?is)^set\b", s.strip())]
    if idx >= len(sets):
        return None
    pos = sets[idx]
    stmt = poc.setup[pos]
    # Recursive CTE inserts need the depth/iteration SET.
    if re.search(r"(?is)cte_max_recursion|max_recursive_iterations", stmt) and any(
        "recursive" in s.lower() for s in poc.setup
    ):
        return None
    # CHECK exclusion oracles require constraint_exclusion=on (PG/OpenGauss).
    if re.search(r"(?is)constraint_exclusion", stmt):
        return None
    out = poc.clone()
    out.setup = poc.setup[:pos] + poc.setup[pos + 1 :]
    out.notes.append(f"dropped SET: {stmt[:60]}")
    return out


def candidates(poc: PocFile, *, skip_clauses: bool) -> Iterable[PocFile]:
    seen: Set[str] = set()

    def emit(nxt: Optional[PocFile]) -> Optional[PocFile]:
        if nxt is None:
            return None
        key = "\n".join(nxt.setup) + "\n" + nxt.query
        if key in seen:
            return None
        seen.add(key)
        return nxt

    nxt = emit(drop_unused_tables(poc))
    if nxt:
        yield nxt
    n_idx = sum(1 for s in poc.setup if is_index_stmt(s))
    for i in range(n_idx):
        nxt = emit(drop_one_index(poc, i))
        if nxt:
            yield nxt
    used_cols = referenced_columns(poc.query)
    for col in reversed(KNOWN_COLS):
        if col in used_cols:
            continue
        nxt = emit(drop_column(poc, col))
        if nxt:
            yield nxt
    nxt = emit(strip_tautologies(poc))
    if nxt:
        yield nxt
    nxt = emit(narrow_select_star(poc))
    if nxt:
        yield nxt
    if skip_clauses:
        return
    # Do not drop WHERE/HAVING conjuncts: that changes true cardinality
    # while the header oracle stays the original query's bound.
    span = _clause_span(poc.query, "order by")
    n = len(split_top_level(poc.query[span[0] : span[1]])) if span else 0
    for i in range(max(0, n - 1)):
        nxt = emit(drop_order_term(poc, i))
        if nxt:
            yield nxt
    n_set = sum(1 for s in poc.setup if re.match(r"(?is)^set\b", s.strip()))
    for i in range(n_set):
        nxt = emit(drop_set_stmt(poc, i))
        if nxt:
            yield nxt


def dummy_spec(poc: PocFile) -> QuerySpec:
    q = poc.query or ""
    having = "1" if re.search(r"(?is)\bhaving\b", q) else None
    group_by = "1" if re.search(r"(?is)\bgroup\s+by\b", q) else None
    return QuerySpec(
        path_kind=poc.path_kind,
        select_list="*",
        from_clause="t0",
        oracle=poc.oracle,
        having=having,
        group_by=group_by,
    )


def apply_setup(adapter: Adapter, setup: Sequence[str]) -> None:
    db = getattr(adapter.cfg, "db", "")
    if db in {"postgres", "cockroach", "opengauss"}:
        for name in ("t0", "t1", "t2", "t3"):
            try:
                adapter.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
            except Exception:
                adapter._rollback_quiet()
    elif db == "oracle":
        for name in ("t0", "t1", "t2", "t3"):
            try:
                adapter.execute(adapter.drop_table_sql(name))
            except Exception:
                adapter._rollback_quiet()
    for stmt in setup:
        sql = stmt
        if db in {"postgres", "cockroach", "opengauss"} and re.match(r"(?is)^drop\s+table\b", sql):
            if not re.search(r"(?i)\bcascade\b", sql):
                sql = sql.rstrip(";") + " CASCADE"
        if db == "oracle" and re.match(r"(?is)^drop\s+table\b", sql):
            # apply_setup already dropped t0–t3; bare DROP TABLE t PURGE fails if absent.
            try:
                adapter.execute(sql)
            except Exception:
                adapter._rollback_quiet()
            continue
        # sqlplus-style EXEC is not valid through the DB-API cursor.
        if db == "oracle" and re.match(r"(?is)^exec(?:ute)?\b", sql):
            call = re.sub(r"(?is)^exec(?:ute)?\s+", "", sql).rstrip(";")
            sql = f"BEGIN {call}; END;"
        adapter.execute(sql)


def check_bug(
    adapter: Adapter,
    poc: PocFile,
    cfg: Config,
) -> Tuple[bool, Optional[ExplainResult], Optional[ExplainResult], Optional[str]]:
    spec = dummy_spec(poc)
    try:
        apply_setup(adapter, poc.setup)
    except Exception as exc:
        if is_connection_error(exc) or is_timeout_error(exc):
            adapter.recover_session()
        return False, None, None, f"setup: {exc}"
    try:
        plan = adapter.explain(poc.query)
    except Exception as exc:
        if is_connection_error(exc) or is_timeout_error(exc):
            adapter.recover_session()
        return False, None, None, f"explain: {exc}"
    executed: Optional[ExplainResult] = None
    if cfg.analyze and adapter.supports_explain_analyze():
        try:
            executed = adapter.explain_analyze(poc.query)
        except Exception as exc:
            if is_timeout_error(exc):
                adapter.recover_session()
                executed = adapter.timed_out_result("EXPLAIN ANALYZE " + poc.query, exc)
            elif poc.had_analyze:
                if is_connection_error(exc):
                    adapter.recover_session()
                return False, plan, None, f"analyze: {exc}"
            else:
                executed = None
    ok = is_violation(spec, plan, cfg, 0, executed)
    if not ok and not poc.had_analyze:
        from cpqs.runner import exceeds_oracle

        ok = exceeds_oracle(spec, plan.access_rows, cfg)
    return ok, plan, executed, None


def render_poc(
    poc: PocFile,
    cfg: Config,
    plan: Optional[ExplainResult],
    executed: Optional[ExplainResult],
) -> str:
    lines = [ln for ln in poc.header if ln.startswith("--")]

    def upsert(prefix: str, value: str) -> None:
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f"{prefix} {value}"
                return
        lines.append(f"{prefix} {value}")

    if plan is not None:
        upsert("-- explained max rows:", str(plan.access_rows))
        upsert("-- explained root rows:", str(plan.root_rows))
    if executed is not None:
        if executed.timed_out:
            lines[:] = [ln for ln in lines if not ln.startswith("-- analyzed actual")]
            upsert("-- analyzed:", f"TIMED OUT after {cfg.timeout_s}s (counts as bug)")
        else:
            lines[:] = [ln for ln in lines if not ln.startswith("-- analyzed: TIMED OUT")]
            upsert("-- analyzed actual max rows:", str(executed.actual_access_rows))
            upsert("-- analyzed actual root rows:", str(executed.actual_root_rows))
    for note in poc.notes:
        marker = f"-- note: minimized: {note}"
        if marker not in lines:
            lines.append(marker)
    lines.append("")
    preamble: List[str] = []
    tables: List[str] = []
    buf: List[str] = []
    for stmt in poc.setup:
        s = stmt.rstrip(";") + ";"
        if re.match(r"(?is)^drop\s+table\b", s):
            if buf:
                tables.extend(buf)
                tables.append("")
                buf = []
            buf.append(s)
        elif re.match(
            r"(?is)^(?:create\s+table|insert\s+into|create\s+(?:unique\s+)?index|analyze\b|exec\b|begin\b|commit\b)",
            s,
        ) or re.search(r"(?is)\bdbms_stats\b", s):
            # Keep ANALYZE / stats / COMMIT after INSERT, not in the SET preamble.
            buf.append(s)
        else:
            preamble.append(s)
            preamble.append("")
    if buf:
        tables.extend(buf)
        tables.append("")
    lines.extend(preamble)
    lines.extend(tables)
    lines.append(poc.query.rstrip(";") + ";")
    lines.append("")
    if plan is not None:
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
    lines.append("")
    return "\n".join(lines)


def minimize_poc(
    poc: PocFile,
    adapter: Adapter,
    cfg: Config,
    *,
    log: Callable[[str], None] = print,
) -> Tuple[PocFile, Optional[ExplainResult], Optional[ExplainResult], str]:
    ok, plan, executed, err = check_bug(adapter, poc, cfg)
    if not ok:
        return poc, plan, executed, err or "original does not reproduce"
    current = poc
    skip_clauses = poc.timed_out
    improved = True
    while improved:
        improved = False
        for cand in candidates(current, skip_clauses=skip_clauses):
            cok, cplan, cexec, cerr = check_bug(adapter, cand, cfg)
            if not cok:
                if cerr:
                    log(f"  reject ({cerr})")
                continue
            log(f"  keep: {cand.notes[-1] if cand.notes else 'reduction'}")
            current = cand
            plan, executed = cplan, cexec
            improved = True
            break
    return current, plan, executed, "ok"
