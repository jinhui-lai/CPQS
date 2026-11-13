from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cpqs.minpoc import (
    drop_column,
    drop_one_index,
    drop_unused_tables,
    parse_poc,
    referenced_columns,
    referenced_tables,
    render_poc,
    split_top_level,
    strip_tautologies,
)
from cpqs.config import Config


MYSQL_TUPLE = """-- CPQS performance / cardinality bug #1
-- access path: tuple_gt
-- expected access cardinality: [6,6] exact
-- explained max rows: 9688

SET SESSION cte_max_recursion_depth = 10010;

DROP TABLE IF EXISTS t0;
CREATE TABLE t0 (c0 INT NOT NULL, c1 INT NOT NULL, k INT NOT NULL, payload INT NOT NULL, PRIMARY KEY (c0), UNIQUE (c1));
INSERT INTO t0 (c0, c1, k, payload) WITH RECURSIVE seq(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 10000) SELECT i, i, MOD(i - 1, 100) + 1, MOD(i * 17, 97) FROM seq;
CREATE INDEX t0_k ON t0 (k);

DROP TABLE IF EXISTS t3;
CREATE TABLE t3 (c0 INT NOT NULL, c1 INT NOT NULL, k INT NOT NULL, payload INT NOT NULL, PRIMARY KEY (c0), UNIQUE (c1));
INSERT INTO t3 (c0, c1, k, payload) WITH RECURSIVE seq(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 10000) SELECT i, i, MOD(i - 1, 100) + 1, MOD(i * 17, 97) FROM seq;
CREATE INDEX t3_k ON t3 (k);

SELECT * FROM t3 WHERE (c0, c1) < (702, 702) ORDER BY c0 DESC, c1 DESC LIMIT 6;
"""

MARIA_TRUE = """-- access path: eq_and_gt_lt+having
-- expected access cardinality: [1,1] exact

DROP TABLE IF EXISTS t2;
CREATE TABLE t2 (c0 INT NOT NULL, c1 INT NOT NULL, PRIMARY KEY (c0));
INSERT INTO t2 (c0, c1) SELECT i, i FROM seq;
SELECT * FROM t2 HAVING c1 = c0 AND c1 > 4455 AND c1 < 4457 AND TRUE;
"""

CH_MAX = """-- access path: boundary_minmax
-- expected access cardinality: [1,1] exact

DROP TABLE IF EXISTS t0;
CREATE TABLE t0 (c0 Int32, k Int32, payload Int32) ENGINE = MergeTree ORDER BY (c0);
INSERT INTO t0 (c0, k, payload) SELECT i, (i - 1) % 100 + 1, (i * 17) % 97 FROM (SELECT number + 1 AS i FROM numbers(10000));
DROP TABLE IF EXISTS t1;
CREATE TABLE t1 (c0 Int32, k Int32, payload Int32) ENGINE = MergeTree ORDER BY (c0);
INSERT INTO t1 (c0, k, payload) SELECT i, (i - 1) % 100 + 1, (i * 17) % 97 FROM (SELECT number + 1 AS i FROM numbers(10000));
SELECT MAX(k) FROM t0;
"""


class MinimizeParseTests(unittest.TestCase):
    def _poc(self, text: str, name: str = "bug-0001-x.sql"):
        path = Path("/tmp") / name
        path.write_text(text, encoding="utf-8")
        return parse_poc(path)

    def test_split_and(self) -> None:
        parts = split_top_level("c1 = c0 AND c1 > 1 AND (a = 1 OR b = 2)", sep=" AND ")
        self.assertEqual(parts, ["c1 = c0", "c1 > 1", "(a = 1 OR b = 2)"])

    def test_mysql_tuple_gt_unused_tables(self) -> None:
        poc = self._poc(MYSQL_TUPLE)
        self.assertEqual(poc.path_kind, "tuple_gt")
        self.assertEqual(poc.oracle.upper_bound, 6)
        self.assertEqual(referenced_tables(poc.query), {"t3"})
        nxt = drop_unused_tables(poc)
        self.assertIsNotNone(nxt)
        tables = referenced_tables("\n".join(nxt.setup))
        self.assertEqual(tables, {"t3"})
        self.assertNotIn("CREATE TABLE t0", "\n".join(nxt.setup))

    def test_drop_payload_column(self) -> None:
        poc = drop_unused_tables(self._poc(MYSQL_TUPLE))
        assert poc is not None
        nxt = drop_column(poc, "payload")
        self.assertIsNotNone(nxt)
        blob = "\n".join(nxt.setup)
        self.assertNotIn("payload", blob.lower())
        self.assertIn("c0", blob)
        self.assertRegex(blob, r"SELECT\s+i,")

    def test_drop_column_strips_check_and_composite_unique(self) -> None:
        text = """-- expected access cardinality: [0,0] exact
-- access path: partition_expr_empty
DROP TABLE IF EXISTS t0;
CREATE TABLE t0 (c0 INT NOT NULL, c1 INT NOT NULL, k INT NOT NULL, part INT NOT NULL, payload INT NOT NULL, PRIMARY KEY (c0, part), UNIQUE (c1, part), CHECK (k >= 1 AND k <= 100), CHECK (payload >= 0 AND payload <= 96), CHECK (k <= c0)) PARTITION BY RANGE (part) (PARTITION p1 VALUES LESS THAN (2), PARTITION pmax VALUES LESS THAN MAXVALUE);
INSERT INTO t0 (c0, c1, k, part, payload) SELECT i, i, 1, 1, 0 FROM seq;
CREATE INDEX t0_k ON t0 (k);
SELECT * FROM t0 WHERE (part + 0) > 10;
"""
        poc = self._poc(text, "bug-0003-partition_expr_empty.sql")
        nxt = drop_column(poc, "k")
        self.assertIsNotNone(nxt)
        blob = "\n".join(nxt.setup)
        self.assertNotRegex(blob, r"(?i)\bk\b")
        self.assertNotIn("t0_k", blob)
        self.assertIn("PRIMARY KEY (c0, part)", blob)
        nxt2 = drop_column(nxt, "c1")
        self.assertIsNotNone(nxt2)
        blob2 = "\n".join(nxt2.setup)
        self.assertNotIn("c1", blob2.lower())
        self.assertNotIn("UNIQUE", blob2.upper())
        nxt3 = drop_column(nxt2, "payload")
        self.assertIsNotNone(nxt3)
        blob3 = "\n".join(nxt3.setup)
        self.assertNotIn("payload", blob3.lower())
        self.assertNotIn("CHECK", blob3.upper())

    def test_strip_true(self) -> None:
        poc = self._poc(MARIA_TRUE)
        self.assertIn("TRUE", poc.query.upper())
        nxt = strip_tautologies(poc)
        self.assertIsNotNone(nxt)
        self.assertNotIn("TRUE", nxt.query.upper())

    def test_clickhouse_max_k(self) -> None:
        poc = self._poc(CH_MAX)
        self.assertEqual(referenced_tables(poc.query), {"t0"})
        self.assertEqual(referenced_columns(poc.query), {"k"})
        nxt = drop_unused_tables(poc)
        self.assertEqual(referenced_tables("\n".join(nxt.setup)), {"t0"})

    def test_drops_unused_partition_children(self) -> None:
        text = """-- expected access cardinality: [1,1] exact
DROP TABLE IF EXISTS t0;
CREATE TABLE t0 (c0 INTEGER NOT NULL, part INT NOT NULL, PRIMARY KEY (c0, part)) PARTITION BY RANGE (part);
CREATE TABLE t0_p1 PARTITION OF t0 FOR VALUES FROM (1) TO (2);
DROP TABLE IF EXISTS t3;
CREATE TABLE t3 (c0 INTEGER NOT NULL, part INT NOT NULL, PRIMARY KEY (c0, part)) PARTITION BY RANGE (part);
CREATE TABLE t3_p1 PARTITION OF t3 FOR VALUES FROM (1) TO (2);
SELECT * FROM t0 WHERE c0 = 1;
"""
        poc = self._poc(text, "bug-0004-join.sql")
        nxt = drop_unused_tables(poc)
        self.assertIsNotNone(nxt)
        blob = "\n".join(nxt.setup)
        self.assertIn("CREATE TABLE t0", blob)
        self.assertIn("t0_p1", blob)
        self.assertNotIn("CREATE TABLE t3", blob)
        self.assertNotIn("t3_p1", blob)

    def test_ignores_oracle_analyze_comment(self) -> None:
        text = """-- expected access cardinality: [6,6] exact
DROP TABLE IF EXISTS t2;
CREATE TABLE t2 (c0 INT NOT NULL, PRIMARY KEY (c0));
SELECT * FROM t2 WHERE c0 IN (1, 2);
-- SELECT /*+ GATHER_PLAN_STATISTICS */ * FROM t2 WHERE c0 IN (1, 2);
"""
        poc = self._poc(text, "bug-0001-full_outer_join.sql")
        self.assertEqual(poc.query.upper().split()[0], "SELECT")
        self.assertNotIn("GATHER_PLAN_STATISTICS", poc.query)
        self.assertEqual(len(poc.setup), 2)

    def test_render_roundtrip_header(self) -> None:
        poc = drop_unused_tables(self._poc(MYSQL_TUPLE))
        text = render_poc(poc, Config(db="mysql"), None, None)
        self.assertIn("SELECT * FROM t3", text)
        self.assertIn("DROP TABLE IF EXISTS t3", text)
        self.assertNotIn("DROP TABLE IF EXISTS t0", text)
        self.assertIn("tuple_gt", text)

    def test_keeps_index_on_query_columns(self) -> None:
        poc = drop_unused_tables(self._poc(MYSQL_TUPLE))
        assert poc is not None
        poc.setup.append("CREATE UNIQUE INDEX t3_c1 ON t3 (c1)")
        self.assertIsNone(drop_one_index(poc, 1))
        dropped = drop_one_index(poc, 0)
        self.assertIsNotNone(dropped)
        blob = "\n".join(dropped.setup)
        self.assertNotIn("CREATE INDEX t3_k", blob)
        self.assertIn("CREATE UNIQUE INDEX t3_c1", blob)

    def test_refuses_pk_column_drop(self) -> None:
        poc = drop_unused_tables(self._poc(MYSQL_TUPLE))
        assert poc is not None
        self.assertIsNone(drop_column(poc, "c0"))


if __name__ == "__main__":
    unittest.main()
