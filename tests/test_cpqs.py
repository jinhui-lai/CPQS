from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cpqs.config import Config
from cpqs.core.oracle import CardinalityOracle, and_oracles, and_residual, or_oracles
from cpqs.db.explain import (
    parse_clickhouse_analyze,
    parse_clickhouse_estimate_rows,
    parse_cockroach_text,
    parse_mysql_json,
    parse_mysql_tree,
    parse_oceanbase_plan,
    parse_oracle_xplan,
    parse_postgres_text,
    parse_tabular_rows,
)
from cpqs.gen.queries import generate_query, gen_desc_ordered_limit, gen_unique_lookup
from cpqs.gen.constraints import gen_null_contradiction, gen_outer_join_elim
from cpqs.gen.partitions import gen_partition_hash_eq, gen_partition_list_eq, gen_partition_local_index
from cpqs.gen.schema import build_catalog, create_table_sql, schema_profiles, secondary_index_sql
from cpqs.core.query import QuerySpec
from cpqs.core.oracle import CardinalityCertificate
from cpqs.runner import is_violation, bug_fingerprint, bug_kind, allowed_rows
from cpqs.db.explain import ExplainResult
import random


class OracleTests(unittest.TestCase):
    def test_exact_and_interval(self) -> None:
        exact = CardinalityOracle.exact_count(3, frozenset({1, 2, 3}))
        self.assertTrue(exact.exact)
        interval = CardinalityOracle.interval(0, 10)
        self.assertFalse(interval.exact)
        self.assertTrue(exact.is_tight(10000, 0.01))
        self.assertFalse(interval.is_tight(100, 0.01))

    def test_and_with_candidate_sets(self) -> None:
        left = CardinalityOracle.exact_count(3, frozenset({1, 2, 3}))
        right = CardinalityOracle.exact_count(2, frozenset({2, 3}))
        both = and_oracles(left, right)
        self.assertEqual(both.lower_bound, 2)
        self.assertEqual(both.candidate_rows, frozenset({2, 3}))

    def test_and_unknown_overlap(self) -> None:
        left = CardinalityOracle.interval(0, 5)
        right = CardinalityOracle.interval(1, 4)
        both = and_oracles(left, right)
        self.assertEqual((both.lower_bound, both.upper_bound), (0, 4))

    def test_residual(self) -> None:
        efficient = CardinalityOracle.exact_count(2, frozenset({1, 2}))
        self.assertEqual(and_residual(efficient, None).upper_bound, 2)
        self.assertEqual(and_residual(efficient, frozenset({2})).lower_bound, 1)

    def test_or_uncontrolled(self) -> None:
        left = CardinalityOracle.interval(0, 80)
        right = CardinalityOracle.interval(0, 80)
        self.assertIsNone(or_oracles(left, right, 100))


class CatalogTests(unittest.TestCase):
    def test_known_multiplicity(self) -> None:
        catalog = build_catalog(1000, 1000, 10)
        t0 = catalog.primary()
        self.assertEqual(t0.n, 1000)
        self.assertEqual(t0.pk_column, "c0")
        self.assertEqual(t0.multiplicity("c0", 1), 1)
        self.assertEqual(t0.multiplicity("c1", 1), 1)
        self.assertEqual(t0.multiplicity("k", 1), 100)
        self.assertEqual(len(t0.rows_in_range("c0", 1, 10)), 10)
        idx = t0.compatible_ordered_index("b", prefix_eq=("a",))
        self.assertIsNotNone(idx)
        self.assertIsNone(t0.compatible_ordered_index("b"))
        self.assertEqual(t0.unique_leading_columns(), ["c0", "c1"])
        self.assertEqual(t0.pair_for_pk(1, ("u0", "u1")), (1, 1))
        # Default profile stripes ``part`` into n_parts=3 ⇒ ~ceil(1000/3) rows each.
        self.assertEqual(len(t0.rows_in_partition(1)), 334)


class QueryGenTests(unittest.TestCase):
    def test_unique_lookup_uses_existing_key(self) -> None:
        catalog = build_catalog(200, 200, 10)
        cfg = Config(db="mysql", rows=200, rows_small=200, distinct_k=10, allow_small_rows=True)
        spec = gen_unique_lookup(catalog, random.Random(1), cfg)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.oracle.upper_bound, 1)
        self.assertIn("=", spec.where or spec.having or "")

    def test_generate_many_kinds(self) -> None:
        catalog = build_catalog(500, 500, 10, n_tables=4)
        cfg = Config(db="mysql", rows=500, rows_small=500, distinct_k=10, allow_small_rows=True)
        rng = random.Random(7)
        kinds = set()
        sqls = []
        for _ in range(400):
            spec = generate_query(catalog, rng, cfg)
            if spec:
                kinds.add(spec.path_kind.split("+")[0])
                sqls.append(spec.sql("mysql"))
        self.assertTrue(any(k in kinds for k in ("unique_lookup", "ordered_range", "unique_in", "unique_eq")))
        self.assertTrue(any("join" in k or "semi" in k or "anti" in k or k.startswith("inner_join") for k in kinds))
        self.assertTrue(
            any(k.startswith("inner_join_") or k.startswith("straight_join_") for k in kinds),
            kinds,
        )
        blob = "\n".join(sqls)
        self.assertTrue("JOIN" in blob.upper())
        from_multi = [s for s in sqls if s.split(" WHERE ")[0].upper().count("JOIN") >= 2]
        self.assertGreaterEqual(
            len(from_multi),
            40,
            f"3-4 table FROM joins too rare: {len(from_multi)}/250 kinds={kinds}",
        )

    def test_eq_and_in_variant(self) -> None:
        from cpqs.gen.queries import gen_unique_eq_and

        catalog = build_catalog(400, 400, 10)
        cfg = Config(db="mysql", rows=400, allow_small_rows=True)
        found = False
        rng = random.Random(3)
        for _ in range(80):
            spec = gen_unique_eq_and(catalog, rng, cfg)
            if spec is None:
                continue
            text = spec.where or spec.having or ""
            if "=" in text and ("IN" in text or "BETWEEN" in text or ">" in text):
                found = True
                self.assertLessEqual(spec.oracle.upper_bound, 8)
                break
        self.assertTrue(found)

    def test_schema_profiles_change_ddl(self) -> None:
        from cpqs.gen.schema import create_table_sql, schema_profiles, secondary_index_sql

        profiles = schema_profiles("mysql")
        ddls = []
        saw_composite = False
        saw_part = False
        for p in profiles:
            cat = build_catalog(50, 50, 10, p, "mysql")
            sql = create_table_sql(cat.primary(), "mysql") + ";" + ";".join(secondary_index_sql(cat.primary()))
            ddls.append(sql)
            self.assertIn("PRIMARY KEY", sql)
            if "PRIMARY KEY (a, b)" in sql or "PRIMARY KEY (u0, u1)" in sql or "UNIQUE INDEX" in sql and "(u0, u1)" in sql:
                saw_composite = True
            if "PARTITION BY RANGE" in sql:
                saw_part = True
                self.assertIn("part", sql)
        self.assertGreater(len(set(ddls)), 3)
        self.assertTrue(saw_composite)
        self.assertTrue(saw_part)

    def test_table_row_counts_min(self) -> None:
        cfg = Config(rows=100, rows_small=50)
        self.assertEqual(cfg.table_row_counts(), (10000, 10000))
        cfg2 = Config(rows=100, rows_small=50, allow_small_rows=True)
        self.assertEqual(cfg2.table_row_counts(), (100, 50))
        cfg3 = Config(rows=20000)
        self.assertEqual(cfg3.table_row_counts(), (20000, 20000))

    def test_multitable_semi_anti_cardinality(self) -> None:
        from cpqs.gen.joins import (
            gen_anti_join,
            gen_multitable_inner,
            gen_natural_join,
            gen_outer_join_chain,
            gen_semi_join,
            gen_semi_join_chain,
            gen_straight_join_chain,
        )

        catalog = build_catalog(200, 200, 10, n_tables=4)
        self.assertEqual([t.name for t in catalog.ordered_tables()], ["t0", "t1", "t2", "t3"])
        cfg = Config(db="mysql", rows=200, allow_small_rows=True, n_tables=4)
        rng = random.Random(11)
        inner = gen_multitable_inner(catalog, rng, cfg, njoin=4)
        self.assertIsNotNone(inner)
        assert inner is not None
        self.assertIn("t2", inner.from_clause)
        self.assertGreaterEqual(inner.from_clause.upper().count("JOIN"), 3)
        self.assertLessEqual(inner.oracle.upper_bound, 6)

        straight = gen_straight_join_chain(catalog, rng, cfg, njoin=4)
        self.assertIsNotNone(straight)
        assert straight is not None
        self.assertEqual(straight.from_clause.count("STRAIGHT_JOIN"), 3)

        semi = gen_semi_join(catalog, rng, cfg)
        self.assertIsNotNone(semi)
        assert semi is not None
        self.assertTrue("EXISTS" in (semi.where or "") or " IN (" in (semi.where or ""))
        self.assertGreater(semi.oracle.upper_bound, 0)

        nested = gen_semi_join_chain(catalog, rng, cfg)
        self.assertIsNotNone(nested)
        assert nested is not None
        self.assertEqual(nested.where.count("EXISTS"), 2)
        self.assertGreater(nested.oracle.upper_bound, 0)

        anti = gen_anti_join(catalog, rng, cfg)
        self.assertIsNotNone(anti)
        assert anti is not None
        self.assertTrue("NOT EXISTS" in (anti.where or "") or "NOT IN" in (anti.where or ""))
        self.assertGreater(anti.oracle.upper_bound, 0)

        natural = gen_natural_join(catalog, rng, cfg)
        self.assertIsNotNone(natural)
        assert natural is not None
        self.assertIn("NATURAL", natural.from_clause.upper())

        outer = None
        for _ in range(20):
            outer = gen_outer_join_chain(catalog, rng, cfg)
            if outer and "t2" in outer.from_clause:
                break
        self.assertIsNotNone(outer)
        assert outer is not None
        self.assertIn("LEFT", outer.from_clause.upper())
        self.assertEqual(outer.oracle.lower_bound, outer.oracle.upper_bound)

        from cpqs.gen.joins import gen_multitable_inner as gen_inner
        mixed = None
        for _ in range(30):
            mixed = gen_inner(catalog, rng, cfg, njoin=3)
            if mixed and mixed.where and (
                "BETWEEN" in mixed.where or " OR " in mixed.where or ">" in mixed.where
                or "BETWEEN" in mixed.from_clause or " OR " in mixed.from_clause or " IN " in mixed.from_clause
            ):
                break
        self.assertIsNotNone(mixed)

    def test_composite_and_partition_oracles(self) -> None:
        from cpqs.gen.schema import SchemaProfile, build_catalog as build
        from cpqs.gen.composite import gen_composite_unique_eq, gen_composite_unique_in
        from cpqs.gen.partitions import gen_partition_and_unique, gen_partition_eq
        from cpqs.gen.schema import create_table_sql

        comp = SchemaProfile(
            "cu", extra_unique=(("u0", "u1"),), extra_unique_as="unique_index"
        )
        catalog = build(200, 200, 10, comp, "mysql", n_tables=2)
        cfg = Config(db="mysql", rows=200, allow_small_rows=True)
        eq = gen_composite_unique_eq(catalog, random.Random(2), cfg)
        self.assertIsNotNone(eq)
        assert eq is not None
        self.assertEqual(eq.oracle.upper_bound, 1)
        self.assertIn("u0", eq.where or "")
        inn = gen_composite_unique_in(catalog, random.Random(4), cfg)
        self.assertIsNotNone(inn)
        assert inn is not None
        self.assertGreaterEqual(inn.oracle.upper_bound, 2)

        from cpqs.gen.composite import gen_composite_independent, independent_pair_pred
        from cpqs.gen.joins import gen_composite_independent_join
        from cpqs.gen.predicates import COL_PRED_KINDS, pick_col_int_filter

        t0 = catalog.primary()
        for i, kind in enumerate(COL_PRED_KINDS):
            filt = pick_col_int_filter(t0, "u0", random.Random(i + 3), kind)
            if filt is None:
                continue
            self.assertEqual(filt.kind, kind)
            for pk in t0.pk_set():
                val = t0.row_by_pk(pk)["u0"]
                self.assertEqual(pk in filt.rows, val in filt.values, kind)

        pair = independent_pair_pred(t0, ("u0", "u1"), random.Random(9), cfg)
        self.assertIsNotNone(pair)
        assert pair is not None
        pred, rows, f0, f1 = pair
        self.assertNotEqual(f0.kind, f1.kind)
        self.assertIn(" AND ", pred)
        scanned = frozenset(
            int(r["c0"])
            for r in t0.rows
            if r["u0"] in f0.values and r["u1"] in f1.values
        )
        self.assertEqual(rows, scanned)
        self.assertEqual(len(rows), len(scanned))

        indep = gen_composite_independent(catalog, random.Random(11), cfg)
        self.assertIsNotNone(indep)
        assert indep is not None
        text = indep.where or indep.having or ""
        self.assertIn(" AND ", text)
        self.assertGreater(indep.oracle.upper_bound, 0)
        self.assertLessEqual(indep.oracle.upper_bound, max(1, int(t0.n * cfg.max_cardinality_ratio)))
        self.assertEqual(indep.oracle.upper_bound, len(indep.oracle.candidate_rows or ()))

        saw_forms = set()
        for seed in range(40):
            spec = gen_composite_independent(catalog, random.Random(seed), cfg)
            if spec is None:
                continue
            blob = spec.where or spec.having or ""
            if " IN " in blob:
                saw_forms.add("in")
            if "BETWEEN" in blob:
                saw_forms.add("between")
            if " > " in blob or " < " in blob or ">=" in blob or "<=" in blob:
                saw_forms.add("range")
            if " = " in blob:
                saw_forms.add("eq")
        self.assertGreaterEqual(len(saw_forms), 3, saw_forms)

        pk_ab = SchemaProfile("cab", pk_keys=("a", "b"))
        cab = build(200, 200, 10, pk_ab, "mysql", n_tables=4)
        jspec = gen_composite_independent_join(cab, random.Random(5), cfg, njoin=3)
        self.assertIsNotNone(jspec)
        assert jspec is not None
        self.assertIn("a", jspec.where or "")
        self.assertIn("b", jspec.where or "")
        self.assertGreaterEqual(jspec.from_clause.upper().count("JOIN"), 2)
        self.assertEqual(jspec.oracle.upper_bound, len(jspec.oracle.candidate_rows or ()))

        part = SchemaProfile("rp", partitioned=True, n_parts=10)
        pc = build(200, 200, 10, part, "mysql", n_tables=2)
        ddl = create_table_sql(pc.primary(), "mysql")
        self.assertIn("PARTITION BY RANGE (part)", ddl)
        self.assertIn("PRIMARY KEY (c0, part)", ddl)
        oracle_ddl = create_table_sql(pc.primary(), "oracle")
        self.assertIn("VALUES LESS THAN (MAXVALUE)", oracle_ddl)
        peq = gen_partition_eq(pc, random.Random(1), cfg)
        self.assertIsNotNone(peq)
        assert peq is not None
        self.assertTrue(peq.skip_tightness)
        self.assertEqual(peq.oracle.upper_bound, 20)
        pand = gen_partition_and_unique(pc, random.Random(3), cfg)
        self.assertIsNotNone(pand)
        assert pand is not None
        self.assertLessEqual(pand.oracle.upper_bound, 6)

    def test_lateral_skipped_on_mariadb_tidb(self) -> None:
        from cpqs.gen.joins import gen_lateral_join

        catalog = build_catalog(80, 80, 10, n_tables=2)
        self.assertIsNone(gen_lateral_join(catalog, random.Random(1), Config(db="mariadb", allow_small_rows=True)))
        self.assertIsNone(gen_lateral_join(catalog, random.Random(1), Config(db="tidb", allow_small_rows=True)))
        mysql = gen_lateral_join(catalog, random.Random(1), Config(db="mysql", allow_small_rows=True))
        self.assertIsNotNone(mysql)
        assert mysql is not None
        self.assertIn("JOIN LATERAL", mysql.from_clause)
        self.assertIn("AS lat", mysql.from_clause)
        for db in ("clickhouse", "oceanbase"):
            self.assertIsNone(gen_lateral_join(catalog, random.Random(1), Config(db=db, allow_small_rows=True)))
        og = gen_lateral_join(catalog, random.Random(1), Config(db="opengauss", allow_small_rows=True))
        self.assertIsNotNone(og)

    def test_new_dialect_generators_and_ddl(self) -> None:
        from cpqs.db import get_adapter
        from cpqs.gen.data import set_based_insert_sql
        from cpqs.gen.joins import gen_full_outer_join, gen_straight_join_chain
        from cpqs.gen.queries import gen_physical_lookup
        from cpqs.gen.schema import SchemaProfile, create_table_sql, secondary_index_sql

        catalog = build_catalog(80, 80, 10, n_tables=2)
        cfg_ob = Config(db="oceanbase", allow_small_rows=True)
        self.assertIsNotNone(gen_straight_join_chain(catalog, random.Random(1), cfg_ob, njoin=2))
        self.assertIsNone(gen_straight_join_chain(catalog, random.Random(1), Config(db="clickhouse", allow_small_rows=True), njoin=2))
        for db in ("clickhouse", "opengauss"):
            spec = gen_full_outer_join(catalog, random.Random(1), Config(db=db, allow_small_rows=True))
            self.assertIsNotNone(spec, db)
        self.assertIsNone(gen_full_outer_join(catalog, random.Random(1), Config(db="oceanbase", allow_small_rows=True)))
        self.assertIsNotNone(gen_physical_lookup(catalog, random.Random(1), Config(db="opengauss", allow_small_rows=True)))
        self.assertIsNone(gen_physical_lookup(catalog, random.Random(1), Config(db="clickhouse", allow_small_rows=True)))

        part = SchemaProfile("rp", partitioned=True, n_parts=10)
        ch = build_catalog(50, 50, 10, part, "clickhouse")
        ch_ddl = create_table_sql(ch.primary(), "clickhouse")
        self.assertIn("ENGINE = MergeTree", ch_ddl)
        self.assertIn("PARTITION BY part", ch_ddl)
        self.assertIn("ORDER BY", ch_ddl)
        self.assertNotIn("UNIQUE", ch_ddl)
        self.assertEqual(secondary_index_sql(ch.primary(), "clickhouse"), [])
        self.assertIn("numbers(", set_based_insert_sql(ch.primary(), "clickhouse", 10))

        ob = build_catalog(50, 50, 10, part, "oceanbase")
        ob_ddl = create_table_sql(ob.primary(), "oceanbase")
        self.assertIn("PARTITION BY RANGE (part)", ob_ddl)
        self.assertIn("PRIMARY KEY (c0, part)", ob_ddl)

        og = build_catalog(50, 50, 10, part, "opengauss")
        og_ddl = create_table_sql(og.primary(), "opengauss")
        self.assertIn("PARTITION BY RANGE (part)", og_ddl)
        self.assertIn("VALUES LESS THAN", og_ddl)
        self.assertEqual(og.primary().extra_ddl, [])
        pg = build_catalog(50, 50, 10, part, "postgres")
        self.assertTrue(any("PARTITION OF" in s for s in pg.primary().extra_ddl))

        for db in ("clickhouse", "opengauss", "oceanbase"):
            adapter = get_adapter(Config(db=db))
            self.assertEqual(adapter.name, db)
            spec = generate_query(catalog, random.Random(3), Config(db=db, rows=80, allow_small_rows=True))
            self.assertIsNotNone(spec, db)

    def test_oracle_limit_sql(self) -> None:
        spec = QuerySpec(
            path_kind="ordered_limit",
            select_list="*",
            from_clause="t0",
            order_by="id ASC",
            limit=10,
            offset=5,
            oracle=CardinalityOracle.exact_count(15),
        )
        self.assertIn("FETCH NEXT 10 ROWS ONLY", spec.sql("oracle"))
        self.assertIn("LIMIT 10 OFFSET 5", spec.sql("mysql"))

    def test_new_cardinality_oracles(self) -> None:
        from cpqs.gen.joins import gen_const_range_push, gen_semi_empty_outer
        from cpqs.gen.queries import (
            gen_large_in_between,
            gen_loose_distinct,
            gen_prefix_order_limit,
            gen_range_then_eq,
            gen_tuple_inequality,
        )

        catalog = build_catalog(400, 400, 10, n_tables=2)
        cfg = Config(db="mysql", rows=400, allow_small_rows=True, distinct_k=10, max_cardinality_ratio=0.1)

        prefix = gen_prefix_order_limit(catalog, random.Random(1), cfg)
        self.assertIsNotNone(prefix)
        assert prefix is not None
        self.assertEqual(prefix.limit, prefix.oracle.upper_bound)
        self.assertIn("ORDER BY", prefix.sql("mysql"))
        self.assertIn("LIMIT", prefix.sql("mysql"))
        self.assertGreater(prefix.oracle.upper_bound, 0)

        rte = gen_range_then_eq(catalog, random.Random(2), cfg)
        self.assertIsNotNone(rte)
        assert rte is not None
        text = rte.where or ""
        self.assertIn(" AND ", text)
        self.assertTrue("BETWEEN" in text or ">=" in text or ">" in text)
        scanned = rte.oracle.candidate_rows
        self.assertEqual(len(scanned or ()), rte.oracle.upper_bound)

        tgt = gen_tuple_inequality(catalog, random.Random(3), cfg)
        self.assertIsNotNone(tgt)
        assert tgt is not None
        self.assertRegex(tgt.where or "", r"\(.+,.+\) [<>] \(")
        self.assertEqual(tgt.limit, tgt.oracle.upper_bound)

        dist = gen_loose_distinct(catalog, random.Random(4), cfg)
        self.assertIsNotNone(dist)
        assert dist is not None
        self.assertEqual(dist.oracle.upper_bound, 10)
        blob = dist.sql("mysql")
        self.assertTrue("DISTINCT" in blob or "GROUP BY" in blob)

        inn = gen_large_in_between(catalog, random.Random(5), cfg)
        self.assertIsNotNone(inn)
        assert inn is not None
        self.assertIn(" IN ", inn.where or "")
        self.assertIn("BETWEEN", inn.where or "")
        self.assertGreaterEqual(inn.oracle.upper_bound, 16)
        self.assertEqual(inn.oracle.upper_bound, len(inn.oracle.candidate_rows or ()))

        empty = gen_semi_empty_outer(catalog, random.Random(6), cfg)
        self.assertIsNotNone(empty)
        assert empty is not None
        self.assertEqual(empty.oracle.upper_bound, 0)
        blob = (empty.where or "") + " " + empty.from_clause
        self.assertTrue(
            "EXISTS" in blob
            or " IN (" in blob
            or "NOT IN" in blob
            or "JOIN" in blob.upper()
            or "," in empty.from_clause,
            blob,
        )

        pushed = gen_const_range_push(catalog, random.Random(7), cfg)
        self.assertIsNotNone(pushed)
        assert pushed is not None
        self.assertIn("JOIN", pushed.from_clause.upper())
        self.assertIn(".b >=", pushed.where or "")
        self.assertGreater(pushed.oracle.upper_bound, 0)
        self.assertEqual(pushed.oracle.upper_bound, len(pushed.oracle.candidate_rows or ()))

    def test_empty_outer_and_indep_filters_and_order_extra(self) -> None:
        from cpqs.gen.joins import gen_independent_unique_filters, gen_semi_empty_outer
        from cpqs.gen.queries import gen_ordered_limit_extra, gen_ordered_limit_join

        catalog = build_catalog(400, 400, 20, n_tables=4)
        cfg = Config(db="mysql", rows=400, allow_small_rows=True, distinct_k=20)
        empty_kinds = set()
        saw_join = False
        saw_semi = False
        for seed in range(80):
            spec = gen_semi_empty_outer(catalog, random.Random(seed), cfg)
            if spec is None:
                continue
            self.assertEqual(spec.oracle.upper_bound, 0)
            empty_kinds.add(spec.path_kind.split("_")[2] if spec.path_kind.startswith("empty_outer_") else spec.path_kind)
            blob = spec.sql("mysql").upper()
            if "JOIN" in blob or "," in spec.from_clause:
                saw_join = True
            if "EXISTS" in blob or " IN (" in spec.sql("mysql"):
                saw_semi = True
        self.assertTrue(saw_join, empty_kinds)
        self.assertTrue(saw_semi, empty_kinds)
        self.assertGreaterEqual(len(empty_kinds), 3, empty_kinds)

        products = []
        styles = set()
        for seed in range(60):
            spec = gen_independent_unique_filters(catalog, random.Random(seed), cfg)
            if spec is None:
                continue
            self.assertGreater(spec.oracle.upper_bound, 0)
            products.append(spec.oracle.upper_bound)
            sql = spec.sql("mysql").upper()
            if "CROSS JOIN" in sql:
                styles.add("cross")
            elif "LEFT JOIN" in sql:
                styles.add("left")
            elif "JOIN" in sql:
                styles.add("inner")
            elif "," in spec.from_clause:
                styles.add("comma")
            self.assertIn(" AND ", spec.where or "")
        self.assertTrue(products)
        self.assertGreaterEqual(len(styles), 2, styles)

        extra = gen_ordered_limit_extra(catalog, random.Random(3), cfg)
        self.assertIsNotNone(extra)
        assert extra is not None
        self.assertIn(",", extra.order_by or "")
        self.assertEqual(extra.limit, extra.oracle.upper_bound - (extra.offset or 0))
        lead = (extra.order_by or "").split(",")[0]
        self.assertTrue(any(c in lead for c in ("c0", "c1")), extra.order_by)

        joined = gen_ordered_limit_join(catalog, random.Random(4), cfg)
        self.assertIsNotNone(joined)
        assert joined is not None
        self.assertIn("JOIN", joined.from_clause.upper())
        self.assertIn(",", joined.order_by or "")
        self.assertEqual(joined.limit, joined.oracle.upper_bound)


class ExplainParseTests(unittest.TestCase):
    def test_mysql_tree_max_not_root(self) -> None:
        text = (
            "-> Limit: 1 row(s)  (cost=100277 rows=1)\n"
            "    -> Covering index scan on t0 using PRIMARY  (cost=100277 rows=999000)\n"
        )
        plan = parse_mysql_tree(text)
        self.assertEqual(plan.root_rows, 1)
        self.assertEqual(plan.max_rows, 999000)
        self.assertIsNone(plan.actual_access_rows)

    def test_postgres_rows(self) -> None:
        text = "Index Only Scan using t1_pkey on t1  (cost=0.42..8.44 rows=1 width=8)"
        plan = parse_postgres_text(text)
        self.assertEqual(plan.access_rows, 1)

    def test_cockroach_estimated(self) -> None:
        text = "    estimated row count: 1,000 (missing stats)"
        plan = parse_cockroach_text(text)
        self.assertEqual(plan.access_rows, 1000)

    def test_oracle_xplan(self) -> None:
        text = (
            "| Id  | Operation          | Name | Rows  | Bytes |\n"
            "|   0 | SELECT STATEMENT   |      |     1 |    13 |\n"
            "|   1 |  SORT AGGREGATE    |      |     1 |    13 |\n"
            "|   2 |   TABLE ACCESS FULL| T0   |   109M|  1357M|\n"
        )
        plan = parse_oracle_xplan(text)
        self.assertEqual(plan.root_rows, 1)
        self.assertEqual(plan.max_rows, 109e6)

    def test_violation_flags_full_scan(self) -> None:
        spec = QuerySpec(
            path_kind="unique_lookup",
            select_list="*",
            from_clause="t0",
            oracle=CardinalityOracle.exact_count(1),
        )
        plan = ExplainResult("EXPLAIN", "", 998968, 998968)
        cfg = Config(db="oracle")
        self.assertFalse(is_violation(spec, plan, cfg, 10000))
        bad_actual = ExplainResult("EXPLAIN ANALYZE", "", 998968, 998968, actual_max_rows=998968)
        self.assertTrue(is_violation(spec, plan, cfg, 10000, bad_actual))
        plan_ok = ExplainResult("EXPLAIN", "", 1, 1)
        self.assertFalse(is_violation(spec, plan_ok, cfg, 10000))

    def test_mysql_analyze_tree_actual_rows(self) -> None:
        text = (
            "-> Limit: 1 row(s)  (cost=100277 rows=1) (actual time=0.02..0.02 rows=1 loops=1)\n"
            "    -> Covering index scan on t0 using PRIMARY  "
            "(cost=100277 rows=999000) (actual time=1.0..2.0 rows=999000 loops=1)\n"
        )
        plan = parse_mysql_tree(text)
        self.assertEqual(plan.root_rows, 1)
        self.assertEqual(plan.max_rows, 999000)
        self.assertEqual(plan.actual_access_rows, 999000)

    def test_analyze_multiplies_loops(self) -> None:
        text = (
            "-> Nested loop inner join  (cost=10 rows=20) "
            "(actual time=1.0..2.0 rows=10 loops=100)\n"
        )
        plan = parse_mysql_tree(text)
        self.assertEqual(plan.actual_access_rows, 1000)

    def test_postgres_analyze_actual_rows(self) -> None:
        text = (
            "Limit  (cost=0.29..8.31 rows=1 width=8) (actual time=0.02..0.02 rows=1 loops=1)\n"
            "  ->  Index Scan using t0_pkey on t0  (cost=0.29..81.75 rows=1753 width=8) "
            "(actual time=0.02..0.50 rows=1753 loops=1)\n"
        )
        plan = parse_postgres_text(text)
        self.assertEqual(plan.access_rows, 1753)
        self.assertEqual(plan.actual_access_rows, 1753)

    def test_postgres_rows_removed_by_filter_counts(self) -> None:
        text = (
            "Seq Scan on t0  (cost=0.00..184.00 rows=50 width=36) "
            "(actual time=0.5..0.5 rows=0.00 loops=1)\n"
            "  Filter: ((k + 0) = 101)\n"
            "  Rows Removed by Filter: 10000\n"
        )
        plan = parse_postgres_text(text)
        self.assertEqual(plan.access_rows, 50)
        self.assertEqual(plan.actual_access_rows, 10000)

    def test_postgres_append_sums_partition_scans(self) -> None:
        text = (
            "Append  (cost=0.00..195.00 rows=51 width=8) "
            "(actual time=0.2..0.7 rows=3334.00 loops=1)\n"
            "  ->  Seq Scan on tp_p1  (cost=0.00..65.00 rows=17 width=8) "
            "(actual time=0.1..0.1 rows=0.00 loops=1)\n"
            "        Filter: ((part + 0) = 2)\n"
            "        Rows Removed by Filter: 3334\n"
            "  ->  Seq Scan on tp_p2  (cost=0.00..65.00 rows=17 width=8) "
            "(actual time=0.0..0.2 rows=3334.00 loops=1)\n"
            "        Filter: ((part + 0) = 2)\n"
            "  ->  Seq Scan on tp_p3  (cost=0.00..65.00 rows=17 width=8) "
            "(actual time=0.1..0.1 rows=0.00 loops=1)\n"
            "        Filter: ((part + 0) = 2)\n"
            "        Rows Removed by Filter: 3332\n"
        )
        plan = parse_postgres_text(text)
        # max(child est)=17, sum(scan est)=51 → access uses sum when larger
        self.assertEqual(plan.access_rows, 51)
        # examined ≈ removed + matching = 3334+3334+3332
        self.assertEqual(plan.actual_access_rows, 3334 + 3334 + 3332)

    def test_mariadb_json_actual_rows(self) -> None:
        payload = {
            "query_block": {
                "select_id": 1,
                "table": {
                    "table_name": "t0",
                    "rows": 10000,
                    "r_rows": 5,
                    "r_loops": 2,
                },
            }
        }
        plan = parse_mysql_json(payload)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.access_rows, 10000)
        self.assertEqual(plan.actual_access_rows, 10)

        filtered = {
            "query_block": {
                "select_id": 1,
                "table": {
                    "table_name": "t3",
                    "rows": 496,
                    "r_rows": 496,
                    "r_loops": 1,
                    "r_filtered": 6.048387097,
                },
            }
        }
        plan_f = parse_mysql_json(filtered)
        self.assertIsNotNone(plan_f)
        assert plan_f is not None
        # Examined rows stay on actual_max; r_filtered only affects root/output.
        self.assertEqual(plan_f.actual_access_rows, 496)
        self.assertAlmostEqual(plan_f.actual_root_rows or 0, 496 * 6.048387097 / 100.0, places=3)

    def test_analyze_dbs_need_actual_overestimate(self) -> None:
        spec = QuerySpec(
            path_kind="unique_lookup",
            select_list="*",
            from_clause="t0",
            oracle=CardinalityOracle.exact_count(1),
        )
        est = ExplainResult("EXPLAIN", "", 998968, 998968)
        cfg = Config(db="mysql")
        self.assertFalse(is_violation(spec, est, cfg, 10000))
        ok_actual = ExplainResult("EXPLAIN ANALYZE", "", 998968, 998968, actual_max_rows=1)
        self.assertFalse(is_violation(spec, est, cfg, 10000, ok_actual))
        bad_actual = ExplainResult("EXPLAIN ANALYZE", "", 998968, 998968, actual_max_rows=998968)
        self.assertTrue(is_violation(spec, est, cfg, 10000, bad_actual))
        timed = ExplainResult("EXPLAIN ANALYZE", "timeout", None, None, timed_out=True)
        self.assertTrue(is_violation(spec, est, cfg, 10000, timed))
        self.assertFalse(is_violation(spec, ExplainResult("EXPLAIN", "", 1, 1), cfg, 10000, timed))
        cfg_pg = Config(db="postgres")
        self.assertTrue(is_violation(spec, est, cfg_pg, 10000, bad_actual))
        cfg_mdb = Config(db="mariadb")
        self.assertFalse(is_violation(spec, est, cfg_mdb, 10000))
        self.assertTrue(is_violation(spec, est, cfg_mdb, 10000, bad_actual))
        for db in ("tidb", "cockroach", "oracle", "clickhouse", "opengauss", "oceanbase"):
            cfg_db = Config(db=db)
            self.assertFalse(is_violation(spec, est, cfg_db, 10000))
            self.assertTrue(is_violation(spec, est, cfg_db, 10000, bad_actual))

    def test_loose_distinct_uses_analyze_root_not_scan(self) -> None:
        """DISTINCT/GROUP BY: root actual rows; child IndexFullScan is not the oracle."""
        spec = QuerySpec(
            path_kind="loose_distinct",
            select_list="DISTINCT k",
            from_clause="t0",
            oracle=CardinalityOracle.exact_count(100),
        )
        est = ExplainResult("EXPLAIN", "", 8000, 10000)
        executed = ExplainResult(
            "EXPLAIN ANALYZE",
            "",
            8000,
            10000,
            actual_root_rows=100,
            actual_max_rows=10000,
        )
        cfg = Config(db="tidb")
        self.assertFalse(is_violation(spec, est, cfg, 10000, executed))
        still_bug = ExplainResult(
            "EXPLAIN ANALYZE",
            "",
            8000,
            10000,
            actual_root_rows=10000,
            actual_max_rows=10000,
        )
        self.assertTrue(is_violation(spec, est, cfg, 10000, still_bug))

        # Access-path probes still use examined max, even when root matches oracle.
        for path_kind, select, oracle_n in (
            ("ordered_limit_extra", "*", 15),
            ("dual_minmax", "MAX(c0), MAX(c1)", 1),
            ("empty_outer_cross_3t", "*", 0),
            ("unique_ge_lt+having", "*", 1),
        ):
            access = QuerySpec(
                path_kind=path_kind,
                select_list=select,
                from_clause="t0",
                having=("c0 = 1" if "+having" in path_kind else None),
                oracle=CardinalityOracle.exact_count(oracle_n),
            )
            scanned = ExplainResult(
                "EXPLAIN ANALYZE",
                "",
                oracle_n,
                10000,
                actual_root_rows=float(oracle_n),
                actual_max_rows=10000,
            )
            self.assertTrue(
                is_violation(access, ExplainResult("EXPLAIN", "", 1, 10000), Config(db="mysql"), 10000, scanned),
                path_kind,
            )

    def test_tidb_analyze_actrows(self) -> None:
        rows = [
            ("HashAgg_8", "1.00", "1", "root"),
            ("└─TableReader_12", "999000.00", "999000", "root"),
            ("  └─TableFullScan_11", "999000.00", "999000", "cop[tikv]"),
        ]
        plan = parse_tabular_rows(rows, row_index=1, id_index=0, actual_row_index=2)
        self.assertEqual(plan.root_rows, 1)
        self.assertEqual(plan.max_rows, 999000)
        self.assertEqual(plan.actual_access_rows, 999000)

    def test_cockroach_analyze_actual_rows(self) -> None:
        text = (
            "  • group (streaming)\n"
            "  │ actual row count: 9\n"
            "  │ estimated row count: 9\n"
            "  └── • scan\n"
            "        actual row count: 500\n"
            "        estimated row count: 1,000 (100% of the table)\n"
        )
        plan = parse_cockroach_text(text)
        self.assertEqual(plan.access_rows, 1000)
        self.assertEqual(plan.actual_access_rows, 500)

    def test_oracle_allstats_a_rows(self) -> None:
        text = (
            "| Id  | Operation          | Name | Starts | E-Rows | A-Rows |   A-Time   | Buffers |\n"
            "|   0 | SELECT STATEMENT   |      |      1 |        |      1 |00:00:00.01 |       6 |\n"
            "|   1 |  SORT AGGREGATE    |      |      1 |      1 |      1 |00:00:00.01 |       6 |\n"
            "|   2 |   TABLE ACCESS FULL| T0   |      1 |     14 |  998968 |00:00:00.20 |    1000 |\n"
        )
        plan = parse_oracle_xplan(text)
        self.assertEqual(plan.root_rows, 1)
        self.assertEqual(plan.max_rows, 14)
        self.assertEqual(plan.actual_access_rows, 998968)

    def test_clickhouse_estimate_and_analyze(self) -> None:
        est = parse_clickhouse_estimate_rows([("cpqs", "t0", 1, 10000, 2)])
        self.assertEqual(est.access_rows, 10000)
        text = (
            "Query summary:\n"
            "  Read:        10.00 thousand rows, 360.00 KB\n"
            "\n"
            "Expression ((Projection + Before ORDER BY))\n"
            "  I/O: rows 1 → 1\n"
            "  └──ReadFromMergeTree\n"
            "         I/O: rows 0 → 1\n"
        )
        plan = parse_clickhouse_analyze(text)
        self.assertEqual(plan.actual_access_rows, 1)
        self.assertEqual(plan.actual_root_rows, 1)
        cartesian = (
            "Expression ((Project names + Projection))\n"
            "│  I/O: rows 3 → 3\n"
            "└──Join (JOIN FillRightFirst)\n"
            "     I/O: rows 20.00 thousand → 100.00 million\n"
        )
        plan2 = parse_clickhouse_analyze(cartesian)
        self.assertEqual(plan2.actual_access_rows, 100_000_000)
        self.assertEqual(plan2.actual_root_rows, 3)

    def test_oceanbase_est_and_act_rows(self) -> None:
        text = (
            "|ID|OPERATOR   |NAME|EST.ROWS|EST.TIME(us)|ACT.ROWS|\n"
            "|0 |HASH JOIN  |    |1       |10          |1       |\n"
            "|1 | TABLE SCAN|t0  |10000   |50          |998968  |\n"
        )
        plan = parse_oceanbase_plan(text)
        self.assertEqual(plan.access_rows, 10000)
        self.assertEqual(plan.actual_access_rows, 998968)

    def test_timeout_error_detection(self) -> None:
        from cpqs.db.base import is_connection_error, is_timeout_error

        self.assertTrue(is_timeout_error(Exception("canceling statement due to statement timeout")))
        self.assertTrue(is_timeout_error(Exception("(3024, 'Query execution was interrupted')")))
        self.assertTrue(is_timeout_error(OSError(3024, "max_execution_time exceeded")))
        self.assertTrue(is_timeout_error(Exception("Timeout exceeded: elapsed 30s, max_execution_time=30")))
        self.assertTrue(is_timeout_error(Exception("ob_query_timeout")))
        self.assertTrue(is_connection_error(Exception("the connection is closed")))
        self.assertTrue(is_connection_error(Exception("(0, '')")))


class FingerprintTests(unittest.TestCase):
    def test_same_join_root_cause_collapses(self) -> None:
        profiles = schema_profiles("mysql")
        catalog_a = build_catalog(200, 200, 10, n_tables=4, profile=profiles[0])
        catalog_b = build_catalog(200, 200, 10, n_tables=4, profile=profiles[1])
        self.assertNotEqual(catalog_a.profile_name, catalog_b.profile_name)
        spec_4t = QuerySpec(
            path_kind="inner_join_4t",
            select_list="*",
            from_clause="t0 JOIN t1 JOIN t2 JOIN t3",
            oracle=CardinalityOracle.exact_count(6),
        )
        spec_3t = QuerySpec(
            path_kind="inner_join_3t",
            select_list="*",
            from_clause="t0 JOIN t1 JOIN t2",
            oracle=CardinalityOracle.exact_count(6),
        )
        spec_straight = QuerySpec(
            path_kind="straight_join_4t",
            select_list="*",
            from_clause="t0 STRAIGHT_JOIN t1",
            oracle=CardinalityOracle.exact_count(6),
        )
        spec_having = QuerySpec(
            path_kind="unique_eq+having",
            select_list="*",
            from_clause="t0",
            oracle=CardinalityOracle.exact_count(1),
        )
        spec_eq = QuerySpec(
            path_kind="unique_eq",
            select_list="*",
            from_clause="t0",
            oracle=CardinalityOracle.exact_count(1),
        )
        self.assertEqual(bug_kind("inner_join_4t"), bug_kind("inner_join_3t"))
        self.assertEqual(bug_kind("inner_join_4t"), bug_kind("straight_join_4t"))
        self.assertEqual(bug_kind("inner_join_4t"), bug_kind("unique_join"))
        self.assertEqual(bug_kind("inner_join_4t"), bug_kind("or_of_eq_join"))
        self.assertEqual(bug_kind("inner_join_4t"), bug_kind("join_eq_and_eq"))
        self.assertEqual(bug_kind("eq_and_gt_le+having"), bug_kind("eq_and_gt_lt"))
        self.assertEqual(bug_fingerprint(catalog_a, spec_4t), bug_fingerprint(catalog_b, spec_3t))
        self.assertEqual(bug_fingerprint(catalog_a, spec_4t), bug_fingerprint(catalog_a, spec_straight))
        self.assertEqual(bug_fingerprint(catalog_a, spec_having), bug_fingerprint(catalog_a, spec_eq))
        self.assertNotEqual(bug_kind("inner_join_4t"), bug_kind("tuple_in_join"))
        self.assertNotEqual(bug_kind("inner_join_4t"), bug_kind("full_outer_join"))
        self.assertNotEqual(bug_kind("dual_minmax"), bug_kind("tuple_gt"))
        self.assertEqual(bug_kind("empty_outer_inner_4t"), bug_kind("empty_outer_semi"))
        self.assertEqual(bug_kind("indep_unique_3t"), bug_kind("indep_unique_2t"))

    def test_generate_skips_known_bug_families(self) -> None:
        catalog = build_catalog(200, 200, 10, n_tables=4)
        cfg = Config(db="mysql", rows=200, allow_small_rows=True)
        rng = random.Random(11)
        skip = {bug_kind("inner_join_4t"), bug_kind("dual_minmax"), bug_kind("straight_join_3t")}
        kinds = set()
        for _ in range(150):
            spec = generate_query(catalog, rng, cfg, skip_bug_kinds=skip)
            if spec is not None:
                kinds.add(bug_kind(spec.path_kind))
        self.assertTrue(kinds)
        self.assertNotIn("unique_join_chain", kinds)
        self.assertNotIn("dual_minmax", kinds)


class SchemaDrivenAccessTests(unittest.TestCase):
    """P0 extensions from schema-driven-access-strategy.md."""

    def test_desc_index_profile_and_oracle(self) -> None:
        profiles = {p.name: p for p in schema_profiles("mysql")}
        self.assertIn("desc_k_index", profiles)
        catalog = build_catalog(300, 300, 10, profile=profiles["desc_k_index"], dialect="mysql")
        t0 = catalog.primary()
        desc = t0.leading_desc_index("k")
        self.assertIsNotNone(desc)
        assert desc is not None
        self.assertEqual(desc.directions, ("DESC",))
        ddl = ";".join(secondary_index_sql(t0, "mysql"))
        self.assertIn("DESC", ddl)
        cfg = Config(db="mysql", rows=300, allow_small_rows=True)
        spec = gen_desc_ordered_limit(catalog, random.Random(3), cfg)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.path_kind, "desc_ordered_limit")
        self.assertLessEqual(spec.oracle.upper_bound, 20)
        self.assertIn("ORDER BY", spec.sql("mysql"))

    def test_null_contradiction_zero_access(self) -> None:
        catalog = build_catalog(200, 200, 10)
        cfg = Config(db="mysql", rows=200, allow_small_rows=True)
        spec = gen_null_contradiction(catalog, random.Random(2), cfg)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.oracle.upper_bound, 0)
        self.assertIn("IS NULL", spec.where or "")
        self.assertEqual(allowed_rows(spec, cfg), 0.0)
        self.assertTrue(any(isinstance(c, CardinalityCertificate) for c in catalog.primary().certificates))

    def test_outer_join_elim_bound_is_left_filter(self) -> None:
        catalog = build_catalog(500, 500, 10, n_tables=2)
        cfg = Config(db="mysql", rows=500, allow_small_rows=True)
        spec = gen_outer_join_elim(catalog, random.Random(5), cfg)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.path_kind, "outer_join_elim")
        self.assertIn("LEFT JOIN", spec.from_clause)
        self.assertLessEqual(spec.oracle.upper_bound, 8)
        self.assertTrue(spec.oracle.is_tight(500, 0.02))

    def test_list_and_hash_partition_profiles(self) -> None:
        profiles = {p.name: p for p in schema_profiles("mysql")}
        list_cat = build_catalog(300, 300, 10, profile=profiles["list_part_int"], dialect="mysql")
        hash_cat = build_catalog(300, 300, 10, profile=profiles["hash_part_int"], dialect="mysql")
        self.assertTrue(list_cat.primary().partitioned)
        self.assertEqual(list_cat.primary().partition.kind, "list")
        self.assertEqual(hash_cat.primary().partition.kind, "hash")
        list_ddl = create_table_sql(list_cat.primary(), "mysql")
        hash_ddl = create_table_sql(hash_cat.primary(), "mysql")
        self.assertIn("PARTITION BY LIST", list_ddl)
        self.assertIn("PARTITION BY HASH", hash_ddl)
        cfg = Config(db="mysql", rows=300, allow_small_rows=True)
        list_spec = gen_partition_list_eq(list_cat, random.Random(1), cfg)
        hash_spec = gen_partition_hash_eq(hash_cat, random.Random(1), cfg)
        local_spec = gen_partition_local_index(list_cat, random.Random(1), cfg)
        self.assertIsNotNone(list_spec)
        self.assertIsNotNone(hash_spec)
        self.assertIsNotNone(local_spec)
        assert list_spec is not None and hash_spec is not None and local_spec is not None
        self.assertEqual(list_spec.path_kind, "partition_list_eq")
        self.assertEqual(hash_spec.path_kind, "partition_hash_eq")
        self.assertEqual(local_spec.path_kind, "partition_local_index")
        # Cockroach skips partitioning entirely.
        cr_cat = build_catalog(300, 300, 10, profile=profiles["list_part_int"], dialect="cockroach")
        self.assertFalse(cr_cat.primary().partitioned)

    def test_expression_and_generated_abs_index(self) -> None:
        from cpqs.gen.access_ext import gen_expr_abs_lookup, gen_expr_abs_range

        profiles = {p.name: p for p in schema_profiles("mysql")}
        mysql_cat = build_catalog(200, 200, 10, profile=profiles["expr_abs_k"], dialect="mysql")
        maria_cat = build_catalog(200, 200, 10, profile=profiles["expr_abs_k"], dialect="mariadb")
        mysql_idx = mysql_cat.primary().expression_indexes()
        self.assertTrue(mysql_idx)
        self.assertTrue(any(i.expression and not i.keys for i in mysql_idx))
        mysql_ddl = ";".join(secondary_index_sql(mysql_cat.primary(), "mysql"))
        self.assertIn("ABS(k)", mysql_ddl)
        # MariaDB uses generated column + ordinary index.
        self.assertTrue(any(c.name == "k_abs" and c.generated_as for c in maria_cat.primary().columns))
        maria_create = create_table_sql(maria_cat.primary(), "mariadb")
        self.assertIn("k_abs", maria_create)
        cfg = Config(db="mysql", rows=200, allow_small_rows=True)
        spec = gen_expr_abs_lookup(mysql_cat, random.Random(1), cfg)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertIn("ABS", (spec.where or "").upper())
        range_spec = gen_expr_abs_range(mysql_cat, random.Random(2), cfg)
        self.assertIsNotNone(range_spec)

    def test_partial_covering_merge_fk(self) -> None:
        from cpqs.gen.access_ext import (
            gen_covering_lookup,
            gen_index_merge_or,
            gen_inner_join_elim_fk,
            gen_partial_pk_lookup,
        )

        profiles = {p.name: p for p in schema_profiles("postgres")}
        part_cat = build_catalog(300, 300, 10, profile=profiles["partial_pk_on_k"], dialect="postgres")
        cover_cat = build_catalog(300, 300, 10, profile=profiles["covering_k_payload"], dialect="postgres")
        merge_cat = build_catalog(300, 300, 10, profile=profiles["dual_ab_indexes"], dialect="mysql")
        fk_cat = build_catalog(300, 300, 10, profile=profiles["fk_chain"], dialect="mysql", n_tables=2)
        self.assertTrue(part_cat.primary().partial_indexes())
        self.assertIn("WHERE", ";".join(secondary_index_sql(part_cat.primary(), "postgres")))
        self.assertTrue(cover_cat.primary().covering_indexes())
        self.assertIn("INCLUDE", ";".join(secondary_index_sql(cover_cat.primary(), "postgres")))
        self.assertIn("FOREIGN KEY", create_table_sql(fk_cat.table("t1"), "mysql"))
        cfg_pg = Config(db="postgres", rows=300, allow_small_rows=True)
        cfg_my = Config(db="mysql", rows=300, allow_small_rows=True)
        self.assertIsNotNone(gen_partial_pk_lookup(part_cat, random.Random(1), cfg_pg))
        self.assertIsNotNone(gen_covering_lookup(cover_cat, random.Random(1), cfg_pg))
        self.assertIsNotNone(gen_index_merge_or(merge_cat, random.Random(1), cfg_my))
        self.assertIsNotNone(gen_inner_join_elim_fk(fk_cat, random.Random(1), cfg_my))

    def test_oracle_mv_profile(self) -> None:
        from cpqs.gen.access_ext import gen_mv_group_rewrite

        profiles = {p.name: p for p in schema_profiles("oracle")}
        cat = build_catalog(200, 200, 10, profile=profiles["mv_group_k"], dialect="oracle")
        t0 = cat.primary()
        self.assertEqual(t0.mv_name, "mv_t0_k")
        self.assertEqual(t0.mv_cardinality, 10)
        self.assertTrue(t0.post_load_ddl)
        self.assertIn("MATERIALIZED VIEW", t0.post_load_ddl[-1])
        cfg = Config(db="oracle", rows=200, allow_small_rows=True)
        spec = gen_mv_group_rewrite(cat, random.Random(1), cfg)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.oracle.upper_bound, 10)
        # Non-Oracle dialects must not emit the certificate query.
        self.assertIsNone(
            gen_mv_group_rewrite(cat, random.Random(1), Config(db="mysql", rows=200, allow_small_rows=True))
        )


if __name__ == "__main__":
    unittest.main()
