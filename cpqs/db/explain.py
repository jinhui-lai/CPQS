from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence


@dataclass
class ExplainResult:
    sql: str
    text: str
    root_rows: Optional[float]
    max_rows: Optional[float]
    lines: List[str] = field(default_factory=list)
    actual_root_rows: Optional[float] = None
    actual_max_rows: Optional[float] = None
    analyze_sql: Optional[str] = None
    analyze_text: Optional[str] = None
    timed_out: bool = False

    @property
    def access_rows(self) -> Optional[int]:
        if self.max_rows is None:
            return None
        return int(math.ceil(self.max_rows))

    @property
    def actual_access_rows(self) -> Optional[int]:
        if self.actual_max_rows is None:
            return None
        return int(math.ceil(self.actual_max_rows))


EST_ROW_KEYS = {"rows", "estrows", "planrows", "rowsexaminedperscan", "rowsproducedperjoin", "cardinality", "estimated_rows"}


def walk_json_rows(obj: Any, acc: List[float], *, actual: bool = False) -> None:
    """Collect estimated or examined (r_rows) cardinalities from JSON plans."""
    if isinstance(obj, dict):
        if actual:
            r = None
            for key in ("r_rows", "r_output_rows", "actual_rows", "actual_row_count"):
                if key in obj:
                    r = _as_float(obj[key])
                    break
            loops = _as_float(obj.get("r_loops") or obj.get("loops") or obj.get("n_loops")) or 1.0
            if r is not None:
                acc.append(r * loops)
        for key, value in obj.items():
            lk = str(key).lower().replace(" ", "")
            if not actual and lk in EST_ROW_KEYS:
                n = _as_float(value)
                if n is not None:
                    acc.append(n)
            walk_json_rows(value, acc, actual=actual)
    elif isinstance(obj, list):
        for item in obj:
            walk_json_rows(item, acc, actual=actual)


def walk_json_output_rows(obj: Any, acc: List[float]) -> None:
    """MariaDB/MySQL JSON: rows after r_filtered (result cardinality per node)."""
    if isinstance(obj, dict):
        r = None
        for key in ("r_output_rows", "actual_rows", "actual_row_count"):
            if key in obj:
                r = _as_float(obj[key])
                break
        if r is None and "r_rows" in obj:
            r = _as_float(obj["r_rows"])
            rf = _as_float(obj.get("r_filtered"))
            if r is not None and rf is not None and 0.0 <= rf < 100.0:
                r = r * (rf / 100.0)
        loops = _as_float(obj.get("r_loops") or obj.get("loops") or obj.get("n_loops")) or 1.0
        if r is not None:
            acc.append(r * loops)
        for value in obj.values():
            walk_json_output_rows(value, acc)
    elif isinstance(obj, list):
        for item in obj:
            walk_json_output_rows(item, acc)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


_EST_ROWS_RE = re.compile(r"rows=(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")
_ACTUAL_ROWS_RE = re.compile(
    r"actual time=[0-9.eE+-]+(?:\.\.[0-9.eE+-]+)? rows=(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?:\s+loops=(\d+(?:\.\d+)?))?",
    re.I,
)
_TIDB_ACT_RE = re.compile(r"actRows[=:\s]+(\d+(?:\.\d+)?)", re.I)
_ORACLE_AROWS_RE = re.compile(r"\bA-Rows[=:\s]+(\d+)", re.I)
_ACTUAL_TIME_RE = re.compile(
    r"actual time=[0-9.eE+-]+\.\.([0-9.eE+-]+)",
    re.I,
)


def examined_rows_for_table(text: Optional[str], table: str) -> Optional[int]:
    """Records examined by operators that name ``table``.

    Repeated invocations are ``rows × loops``. Lines that do not mention the
    table are ignored, so a second join input can be checked on its own.
    """
    if not text or not table:
        return None
    name = re.compile(rf"\b{re.escape(table)}\b", re.I)
    best: Optional[float] = None
    for raw in text.splitlines():
        if name.search(raw) is None:
            continue
        total: Optional[float] = None
        act = _ACTUAL_ROWS_RE.search(raw)
        if act:
            total = float(act.group(1)) * float(act.group(2) or 1.0)
        else:
            tidb = _TIDB_ACT_RE.search(raw)
            if tidb:
                total = float(tidb.group(1))
            else:
                oracle = _ORACLE_AROWS_RE.search(raw)
                if oracle:
                    total = float(oracle.group(1))
        if total is None:
            continue
        best = total if best is None else max(best, total)
    if best is None:
        return None
    return int(math.ceil(best))


def max_actual_time_ms(text: Optional[str]) -> Optional[float]:
    """Largest ``actual time=..end`` in an ANALYZE plan, in milliseconds."""
    if not text:
        return None
    found = [float(match) for match in _ACTUAL_TIME_RE.findall(text)]
    if not found:
        return None
    return max(found)


def _line_indent_before_arrow(raw: str) -> int:
    arrow = raw.find("->")
    span = raw[:arrow] if arrow >= 0 else raw
    indent = 0
    for ch in span:
        if ch in " \t":
            indent += 1
    return indent


def _split_est_actual(raw: str) -> tuple[str, str]:
    marker = "(actual"
    idx = raw.lower().find(marker)
    if idx < 0:
        return raw, ""
    return raw[:idx], raw[idx:]


def parse_mysql_tree(text: str) -> ExplainResult:
    """Parse EXPLAIN / EXPLAIN ANALYZE FORMAT=TREE. Estimated rows stay in max_rows;
    ANALYZE actual rows*loops go to actual_max_rows."""
    root_rows = None
    max_rows = None
    actual_root = None
    actual_max = None
    best_indent = None
    lines = []
    for raw in text.splitlines():
        lines.append(raw)
        if "rows=" not in raw:
            continue
        if "->" not in raw:
            continue
        indent = _line_indent_before_arrow(raw)
        est_part, act_part = _split_est_actual(raw)
        est_matches = _EST_ROWS_RE.findall(est_part)
        if est_matches:
            n = float(est_matches[-1])
            max_rows = n if max_rows is None else max(max_rows, n)
            if best_indent is None or indent < best_indent:
                best_indent = indent
                root_rows = n
        act = _ACTUAL_ROWS_RE.search(act_part)
        if act:
            rows = float(act.group(1))
            loops = float(act.group(2) or 1.0)
            total = rows * loops
            actual_max = total if actual_max is None else max(actual_max, total)
            if actual_root is None or (best_indent is not None and indent <= best_indent):
                actual_root = total
    return ExplainResult(
        "EXPLAIN",
        text,
        root_rows,
        max_rows,
        lines,
        actual_root_rows=actual_root,
        actual_max_rows=actual_max,
    )


def parse_mysql_json(text: Any) -> Optional[ExplainResult]:
    if isinstance(text, dict):
        payload = text
        raw = json.dumps(text)
    else:
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8")
        try:
            payload = json.loads(text)
            if isinstance(payload, str):
                payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return None
        raw = str(text)
    rows: List[float] = []
    walk_json_rows(payload, rows)
    actual: List[float] = []
    walk_json_rows(payload, actual, actual=True)
    outputs: List[float] = []
    walk_json_output_rows(payload, outputs)
    if not rows and not actual:
        return None
    return ExplainResult(
        "EXPLAIN",
        raw,
        rows[0] if rows else None,
        max(rows) if rows else None,
        [raw],
        actual_root_rows=outputs[0] if outputs else (actual[0] if actual else None),
        actual_max_rows=max(actual) if actual else None,
    )


def parse_tabular_rows(
    rows: Sequence[Sequence[Any]],
    row_index: int,
    id_index: int = 0,
    actual_row_index: Optional[int] = None,
) -> ExplainResult:
    """Generic tabular EXPLAIN: take max of a numeric column; root is least-indented id.

    TiDB EXPLAIN ANALYZE puts estRows then actRows; pass actual_row_index=2.
    """
    max_rows = None
    root_rows = None
    actual_max = None
    actual_root = None
    best_indent = None
    lines = []
    for row in rows:
        lines.append(" | ".join("" if c is None else str(c) for c in row))
        ident = str(row[id_index]) if id_index < len(row) and row[id_index] is not None else ""
        indent = 0
        for ch in ident:
            if ch in " \t│├└─|":
                indent += 1
            else:
                break
        n = _as_float(row[row_index]) if row_index < len(row) else None
        a = None
        if actual_row_index is not None and actual_row_index < len(row):
            a = _as_float(row[actual_row_index])
        if n is None and a is None:
            continue
        if n is not None:
            max_rows = n if max_rows is None else max(max_rows, n)
            if best_indent is None or indent < best_indent:
                best_indent = indent
                root_rows = n
        if a is not None:
            actual_max = a if actual_max is None else max(actual_max, a)
            if actual_root is None or (best_indent is not None and indent <= best_indent):
                actual_root = a
    return ExplainResult(
        "EXPLAIN",
        "\n".join(lines),
        root_rows,
        max_rows,
        lines,
        actual_root_rows=actual_root,
        actual_max_rows=actual_max,
    )


_PG_REMOVED_RE = re.compile(
    r"Rows Removed by (?:Join )?Filter:\s*(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)",
    re.I,
)


_PG_SCAN_HINT_RE = re.compile(
    r"\b(Seq Scan|Index Scan|Index Only Scan|Bitmap Heap Scan|Bitmap Index Scan|"
    r"Partitioned Seq Scan|Partitioned Index|Parallel Seq Scan)\b",
    re.I,
)


def parse_postgres_text(text: str) -> ExplainResult:
    """Parse EXPLAIN / EXPLAIN ANALYZE text.

    ``Rows Removed by Filter`` / ``Join Filter`` count toward actual access:
    a Seq Scan that rejects every row still examined N tuples (CHECK / residual
    proof misses), even though ``actual rows=0``.

    Under ``Append`` / partition plans, also take the **sum** of per-scan
    examined rows so a prune miss that touches every child is not hidden by
    ``max(child) ≈ |one partition|``.
    """
    root_rows = None
    max_rows = None
    actual_root = None
    actual_max = None
    est_scan_sum = 0.0
    actual_scan_sum = 0.0
    lines = []
    for raw in text.splitlines():
        lines.append(raw)
        removed = _PG_REMOVED_RE.search(raw)
        if removed:
            removed_n = float(removed.group(1))
            actual_max = removed_n if actual_max is None else max(actual_max, removed_n)
            # Sibling line under a scan/join; include in Append-aware sum.
            actual_scan_sum += removed_n
        if "rows=" not in raw:
            continue
        est_part, act_part = _split_est_actual(raw)
        est = _EST_ROWS_RE.search(est_part)
        if est:
            n = float(est.group(1))
            if root_rows is None:
                root_rows = n
            max_rows = n if max_rows is None else max(max_rows, n)
            if _PG_SCAN_HINT_RE.search(raw):
                est_scan_sum += n
        act = _ACTUAL_ROWS_RE.search(act_part)
        if act:
            rows = float(act.group(1))
            loops = float(act.group(2) or 1.0)
            total = rows * loops
            if actual_root is None:
                actual_root = total
            actual_max = total if actual_max is None else max(actual_max, total)
            if _PG_SCAN_HINT_RE.search(raw):
                actual_scan_sum += total
    if est_scan_sum > 0 and (max_rows is None or est_scan_sum > max_rows):
        max_rows = est_scan_sum
    if actual_scan_sum > 0 and (actual_max is None or actual_scan_sum > actual_max):
        actual_max = actual_scan_sum
    return ExplainResult(
        "EXPLAIN",
        text,
        root_rows,
        max_rows,
        lines,
        actual_root_rows=actual_root,
        actual_max_rows=actual_max,
    )


_CRDB_EST_RE = re.compile(r"estimated row count:\s*([\d,.eE+-]+)", re.I)
_CRDB_ACT_RE = re.compile(r"actual row count:\s*([\d,.eE+-]+)", re.I)


def parse_cockroach_text(text: str) -> ExplainResult:
    root_rows = None
    max_rows = None
    actual_root = None
    actual_max = None
    lines = []
    for raw in text.splitlines():
        lines.append(raw)
        if "• norows" in raw and root_rows is None:
            root_rows = 0.0
            max_rows = 0.0 if max_rows is None else max_rows
        act = _CRDB_ACT_RE.search(raw)
        if act:
            a = _as_float(act.group(1))
            if a is not None:
                if actual_root is None:
                    actual_root = a
                actual_max = a if actual_max is None else max(actual_max, a)
        est = _CRDB_EST_RE.search(raw)
        if est:
            n = _as_float(est.group(1))
            if n is not None:
                if root_rows is None:
                    root_rows = n
                max_rows = n if max_rows is None else max(max_rows, n)
    return ExplainResult(
        "EXPLAIN",
        text,
        root_rows,
        max_rows,
        lines,
        actual_root_rows=actual_root,
        actual_max_rows=actual_max,
    )


def _oracle_cell_num(text: str) -> Optional[float]:
    cleaned = text.strip().replace(",", "").replace("M", "e6").replace("K", "e3")
    if not cleaned or cleaned in {"-", "-----"}:
        return None
    return _as_float(cleaned)


def parse_oracle_xplan(text: str) -> ExplainResult:
    """Parse DBMS_XPLAN TYPICAL (Rows) or ALLSTATS LAST (E-Rows / A-Rows).

    For ALLSTATS, a FILTER-proved-empty child has ``Starts=0`` (no work). A
    ``TABLE ACCESS FULL`` with ``Starts>=1`` and ``A-Rows=0`` still examined
    the segment (CHECK / residual miss); contribute ``max(A-Rows, Buffers, E-Rows)
    * Starts`` so empty full-scans are not under-counted as 0.
    """
    rows_found: List[float] = []
    actual_found: List[float] = []
    lines = []
    est_idx: Optional[int] = None
    act_idx: Optional[int] = None
    starts_idx: Optional[int] = None
    buffers_idx: Optional[int] = None
    op_idx: Optional[int] = None
    for raw in text.splitlines():
        lines.append(raw)
        if not raw.strip().startswith("|"):
            continue
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 5:
            continue
        headers = [p.lower().replace(" ", "") for p in parts]
        if "id" in headers and any(h in {"rows", "e-rows", "erows"} for h in headers):
            for i, h in enumerate(headers):
                if h in {"rows", "e-rows", "erows"}:
                    est_idx = i
                if h in {"a-rows", "arows"}:
                    act_idx = i
                if h == "starts":
                    starts_idx = i
                if h == "buffers":
                    buffers_idx = i
                if h == "operation":
                    op_idx = i
            continue
        if not parts[1] or parts[1] in {"Id"} or set(parts[1]) <= {"-", "*"}:
            continue
        if est_idx is None:
            est_idx = 4
        e_rows = None
        if est_idx < len(parts):
            e_rows = _oracle_cell_num(parts[est_idx])
            if e_rows is not None:
                rows_found.append(e_rows)
        a_rows = None
        if act_idx is not None and act_idx < len(parts):
            a_rows = _oracle_cell_num(parts[act_idx])
        starts = 1.0
        if starts_idx is not None and starts_idx < len(parts):
            s = _oracle_cell_num(parts[starts_idx])
            if s is not None:
                starts = s
        buffers = None
        if buffers_idx is not None and buffers_idx < len(parts):
            buffers = _oracle_cell_num(parts[buffers_idx])
        op = parts[op_idx].upper() if op_idx is not None and op_idx < len(parts) else ""
        if a_rows is not None:
            examined = a_rows * starts
            # Empty full-scan after filter: A-Rows=0 but Starts>0 still read blocks.
            if (
                starts > 0
                and a_rows == 0
                and ("TABLE ACCESS FULL" in op or "INDEX FAST FULL SCAN" in op)
            ):
                examined = starts * max(e_rows or 0.0, buffers or 0.0, 1.0)
            actual_found.append(examined)
    root = rows_found[0] if rows_found else None
    mx = max(rows_found) if rows_found else None
    return ExplainResult(
        "EXPLAIN",
        text,
        root,
        mx,
        lines,
        actual_root_rows=actual_found[0] if actual_found else None,
        actual_max_rows=max(actual_found) if actual_found else None,
    )


_HUMAN_COUNT_RE = re.compile(
    r"([0-9]*\.?[0-9]+)\s*(thousand|million|billion|k|m|b)?",
    re.I,
)
_CH_IO_RE = re.compile(
    r"I/O:\s*rows\s+(\d[\d.,]*(?:\s*(?:thousand|million|billion|[kmb]))?)\s*(?:→|->)\s+(\d[\d.,]*(?:\s*(?:thousand|million|billion|[kmb]))?)",
    re.I,
)
_CH_READ_RE = re.compile(r"Read:\s+(.+?)\s+rows", re.I)
_CH_OUTPUT_RE = re.compile(r"(?:Output|Result):\s+(.+?)\s+rows", re.I)
_OB_TRACE_ROWS_RE = re.compile(r"(?:act(?:ual)?\.?rows|\brows)\s*[=:]\s*([\d.eE+]+)", re.I)


def _human_count(text: str) -> Optional[float]:
    raw = text.strip().replace(",", "")
    m = _HUMAN_COUNT_RE.match(raw)
    if not m:
        return _as_float(raw)
    n = float(m.group(1))
    suf = (m.group(2) or "").lower()
    mul = {
        "": 1.0,
        "k": 1e3,
        "thousand": 1e3,
        "m": 1e6,
        "million": 1e6,
        "b": 1e9,
        "billion": 1e9,
    }
    return n * mul.get(suf, 1.0)


def parse_clickhouse_estimate_rows(rows: Sequence[Sequence[Any]]) -> ExplainResult:
    """EXPLAIN ESTIMATE: columns database, table, parts, rows, marks."""
    found: List[float] = []
    lines = []
    for row in rows:
        lines.append(" | ".join("" if c is None else str(c) for c in row))
        if not row:
            continue
        n = None
        if len(row) >= 4:
            n = _as_float(row[3])
        if n is None:
            for cell in row:
                n = _as_float(cell)
                if n is not None and n >= 0:
                    break
        if n is not None:
            found.append(n)
    mx = max(found) if found else None
    return ExplainResult("EXPLAIN ESTIMATE", "\n".join(lines), mx, mx, lines)


def parse_clickhouse_analyze(text: str) -> ExplainResult:
    """EXPLAIN ANALYZE pretty plan: operator ``I/O: rows X → Y``.

    The Query-summary ``Read:`` line is ignored: ClickHouse often reports
    uncompressed/part rows there even when PrimaryKey I/O is 1.
    """
    actual: List[float] = []
    root_out: Optional[float] = None
    lines = []
    in_summary = False
    for raw in text.splitlines():
        lines.append(raw)
        stripped = raw.strip()
        if stripped.startswith("Query summary"):
            in_summary = True
            continue
        if in_summary:
            if stripped == "":
                in_summary = False
            continue
        io = _CH_IO_RE.search(raw)
        if io:
            left = _human_count(io.group(1).strip(" ·."))
            right = _human_count(io.group(2).strip(" ·."))
            for n in (left, right):
                if n is not None:
                    actual.append(n)
            if root_out is None and right is not None:
                root_out = right
        elif _CH_OUTPUT_RE.search(raw):
            n = _human_count(_CH_OUTPUT_RE.search(raw).group(1))
            if n is not None:
                actual.append(n)
                if root_out is None:
                    root_out = n
    mx = max(actual) if actual else None
    return ExplainResult(
        "EXPLAIN ANALYZE",
        text,
        mx,
        mx,
        lines,
        actual_root_rows=root_out if root_out is not None else mx,
        actual_max_rows=mx,
    )


def parse_oceanbase_plan(text: str) -> ExplainResult:
    """OceanBase MySQL-mode EXPLAIN ASCII: ID | OPERATOR | NAME | EST.ROWS | ACT.ROWS."""
    found: List[float] = []
    actual: List[float] = []
    lines = []
    est_idx: Optional[int] = None
    act_idx: Optional[int] = None
    for raw in text.splitlines():
        lines.append(raw)
        if "|" not in raw:
            for m in _OB_TRACE_ROWS_RE.finditer(raw):
                n = _as_float(m.group(1))
                if n is not None:
                    actual.append(n)
            continue
        parts = [p.strip() for p in raw.split("|")]
        headers = [p.lower().replace(" ", "").replace(".", "") for p in parts]
        if any(h in {"estrows", "id"} for h in headers) and any(
            h in {"estrows", "actrows", "actualrows", "rows"} for h in headers
        ):
            for i, h in enumerate(headers):
                if h in {"estrows", "estimatedrows"}:
                    est_idx = i
                if h in {"actrows", "actualrows", "arows"}:
                    act_idx = i
                if h == "rows" and est_idx is None:
                    est_idx = i
            continue
        if set(raw.strip()) <= {"=", "-", "|", "+", " "}:
            continue
        if est_idx is None:
            est_idx = 4
        if est_idx < len(parts):
            n = _as_float(parts[est_idx].replace(",", ""))
            if n is not None:
                found.append(n)
        if act_idx is not None and act_idx < len(parts):
            a = _as_float(parts[act_idx].replace(",", ""))
            if a is not None:
                actual.append(a)
        else:
            for m in _OB_TRACE_ROWS_RE.finditer(raw):
                n = _as_float(m.group(1))
                if n is not None:
                    actual.append(n)
    mx = max(found) if found else None
    root = found[0] if found else None
    amx = max(actual) if actual else None
    return ExplainResult(
        "EXPLAIN",
        text,
        root,
        mx,
        lines,
        actual_root_rows=actual[0] if actual else None,
        actual_max_rows=amx,
    )
