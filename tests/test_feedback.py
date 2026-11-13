"""Feedback reward, bound-aware mutations, and the two-level scheduler."""

from __future__ import annotations

import random
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cpqs.config import Config
from cpqs.core.feedback import (
    ArmStat,
    Explorer,
    Feedback,
    discrepancy,
    keep_parent,
    mean_reward,
    plan_signature,
    scalarized_reward,
    score_execution,
    select_arm,
    update_arm,
)
from cpqs.core.feedback import Parent
from cpqs.core.query import AccessOccurrence, QuerySpec
from cpqs.core.oracle import CardinalityOracle
from cpqs.gen.mutate import (
    QUERY_ARMS,
    MutationMemory,
    ast_fingerprint,
    propose,
    reject_unsupported_wrap,
    seed_query,
)
from cpqs.gen.queries import gen_unique_in, gen_unique_lookup
from cpqs.db.explain import ExplainResult, examined_rows_for_table, max_actual_time_ms
from cpqs.gen.schema import build_catalog, schema_profiles
from cpqs.runner import is_violation, occurrence_violation
from cpqs.gen.state_mut import STATE_ARMS, DbState, apply_state_mutation


def _cfg(**kwargs) -> Config:
    base = dict(
        db="postgres",
        rows=400,
        rows_small=400,
        distinct_k=20,
        allow_small_rows=True,
        n_tables=3,
        max_cardinality_ratio=0.05,
    )
    base.update(kwargs)
    return Config(**base)


class RewardTests(unittest.TestCase):
    def test_discrepancy_matches_the_piecewise_definition(self) -> None:
        tau = 8.0
        self.assertEqual(discrepancy(None, 3, tau), 0.0)
        self.assertEqual(discrepancy(0, 0, tau), 0.0)
        self.assertEqual(discrepancy(4, 0, tau), 1.0)
        # ρ = τ ⇒ d = 1/2, and (2/3)d equals the maximum novelty term 1/3.
        self.assertAlmostEqual(discrepancy(16, 2, tau), 0.5)
        self.assertAlmostEqual(scalarized_reward(0.5, 0), 1.0 / 3.0)
        self.assertAlmostEqual(scalarized_reward(0.5, 1), 2.0 / 3.0)

    def test_novelty_is_recorded_after_the_bit_is_read(self) -> None:
        seen = set()
        first = score_execution(1, 1, 8, "index scan t0", seen)
        second = score_execution(1, 1, 8, "index scan t0", seen)
        self.assertEqual(first.g, 1)
        self.assertEqual(second.g, 0)
        self.assertEqual(first.m, 1)
        missing = score_execution(None, 1, 8, None, seen)
        self.assertEqual((missing.d, missing.g, missing.m), (0.0, 0, 0))

    def test_plan_signature_drops_constants_and_keeps_access_methods(self) -> None:
        index = plan_signature(
            "Index Scan using t0_pkey on t0  (cost=0.29..8.31 rows=1 width=36) "
            "Index Cond: (c0 = 1)"
        )
        seq = plan_signature(
            "Seq Scan on t0  (cost=0.00..184.00 rows=10000 width=36) Filter: (c0 = 1)"
        )
        other = plan_signature(
            "Index Scan using t0_pkey on t0  (cost=0.29..8.31 rows=1 width=36) "
            "Index Cond: (c0 = 99)"
        )
        self.assertIsNotNone(index)
        self.assertEqual(index, other)
        self.assertNotEqual(index, seq)
        assert index is not None and seq is not None
        self.assertIn("index scan", index)
        self.assertIn("seq scan", seq)

    def test_ucb_explores_unpulled_arms_before_exploitation(self) -> None:
        stats = {name: ArmStat() for name in ("a", "b", "c")}
        update_arm(stats["a"], 1.0)
        self.assertEqual(select_arm(stats, ("a", "b", "c"), ("a", "b")), "b")
        update_arm(stats["b"], 0.0)
        # t = 1+2 = 3. a has a higher mean and still wins against an unrewarded b.
        chosen = select_arm(stats, ("a", "b", "c"), ("a", "b"))
        self.assertEqual(chosen, "a")

    def test_keep_drops_unmeasurable_and_low_reward_parents(self) -> None:
        pool = keep_parent([], "q", "k", 0.4, 0, 2)
        self.assertEqual(pool, [])
        pool = keep_parent(pool, "q1", "k1", 0.2, 1, 2)
        pool = keep_parent(pool, "q2", "k2", 0.9, 1, 2)
        pool = keep_parent(pool, "q3", "k3", 0.1, 1, 2)
        keys = {parent.key for parent in pool}
        self.assertEqual(keys, {"k1", "k2"})
        pool = keep_parent(pool, "q2b", "k2", 0.95, 1, 2)
        found = next(parent for parent in pool if parent.key == "k2")
        self.assertEqual(found.payload, "q2b")


class MutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _cfg()
        self.catalog = build_catalog(400, 400, 20, n_tables=3, dialect="postgres")
        self.rng = random.Random(1)

    def test_seed_covers_aggregate_and_join_elimination_primitives(self) -> None:
        found = set()
        cursor = [0]
        for _ in range(40):
            spec = seed_query(self.catalog, random.Random(1), self.cfg, cursor)
            if spec:
                found.add(spec.path_kind)
        self.assertIn("dual_minmax", found)
        self.assertIn("outer_join_elim", found)
        self.assertIn("unsat_range", found)

    def test_seed_is_selective_and_anchored(self) -> None:
        spec = seed_query(self.catalog, self.rng, self.cfg, [0])
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.bound_outcome, "seed")
        self.assertTrue(spec.pivot_table)
        self.assertLessEqual(spec.oracle.upper_bound, 200)

    def test_equality_to_in_preserves_the_bound(self) -> None:
        spec = gen_unique_lookup(self.catalog, self.rng, self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        memory = MutationMemory()
        child = propose("expr_eq_to_in", [Parent(ast_fingerprint(spec, "postgres"), 0.2, spec)], self.catalog, self.cfg, memory, "s", True)
        self.assertIsNotNone(child)
        assert child is not None
        self.assertEqual(child.bound_outcome, "preservation")
        self.assertEqual(child.oracle.upper_bound, spec.oracle.upper_bound)
        self.assertEqual(child.oracle.candidate_rows, spec.oracle.candidate_rows)
        self.assertIn(" IN ", child.where or "")

    def test_shrink_tightens_and_or_composes(self) -> None:
        spec = gen_unique_in(self.catalog, random.Random(2), self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        parent = Parent(ast_fingerprint(spec, "postgres"), 0.4, spec)
        memory = MutationMemory()
        shrunk = propose("expr_shrink", [parent], self.catalog, self.cfg, memory, "s", True)
        self.assertIsNotNone(shrunk)
        assert shrunk is not None
        self.assertEqual(shrunk.bound_outcome, "tightening")
        self.assertLess(shrunk.oracle.upper_bound, spec.oracle.upper_bound)
        assert shrunk.oracle.candidate_rows is not None and spec.oracle.candidate_rows is not None
        self.assertTrue(shrunk.oracle.candidate_rows < spec.oracle.candidate_rows)
        composed = propose(
            "expr_or_compose",
            [Parent(ast_fingerprint(spec, "postgres"), 0.4, spec)],
            self.catalog,
            self.cfg,
            MutationMemory(),
            "s",
            True,
        )
        self.assertIsNotNone(composed)
        assert composed is not None
        self.assertEqual(composed.bound_outcome, "composition")
        self.assertGreater(composed.oracle.upper_bound, spec.oracle.upper_bound)
        self.assertLessEqual(composed.oracle.upper_bound, int(400 * self.cfg.max_cardinality_ratio))

    def test_in_or_round_trip_preserves_rows(self) -> None:
        spec = gen_unique_in(self.catalog, random.Random(3), self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        memory = MutationMemory()
        as_or = propose("expr_in_to_or", [Parent("p", 0.1, spec)], self.catalog, self.cfg, memory, "s", True)
        assert as_or is not None
        self.assertEqual(as_or.bound_outcome, "preservation")
        back = propose("expr_or_to_in", [Parent("o", 0.1, as_or)], self.catalog, self.cfg, MutationMemory(), "s", True)
        assert back is not None
        self.assertEqual(back.oracle.candidate_rows, spec.oracle.candidate_rows)

    def test_order_limit_tightens_a_range(self) -> None:
        from cpqs.gen.queries import gen_ordered_range

        found = None
        rng = random.Random(4)
        for _ in range(40):
            spec = gen_ordered_range(self.catalog, rng, self.cfg)
            if spec is not None and spec.oracle.upper_bound >= 2 and "BETWEEN" in (spec.where or ""):
                spec.pivot_table = spec.from_clause.strip()
                found = spec
                break
        self.assertIsNotNone(found)
        assert found is not None
        child = propose(
            "ast_order_limit",
            [Parent("p", 0.2, found)],
            self.catalog,
            self.cfg,
            MutationMemory(),
            "s",
            True,
        )
        self.assertIsNotNone(child)
        assert child is not None
        self.assertEqual(child.bound_outcome, "tightening")
        self.assertLess(child.oracle.upper_bound, found.oracle.upper_bound)
        self.assertIsNotNone(child.limit)

    def test_join_preserves_per_input_bound(self) -> None:
        spec = gen_unique_in(self.catalog, random.Random(5), self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        child = propose("ast_inner_join", [Parent("p", 0.3, spec)], self.catalog, self.cfg, MutationMemory(), "s", True)
        self.assertIsNotNone(child)
        assert child is not None
        self.assertEqual(child.bound_outcome, "preservation")
        self.assertEqual(child.oracle.upper_bound, spec.oracle.upper_bound)
        self.assertIn("INNER JOIN", child.from_clause)
        widened = propose("expr_join_or", [Parent("j", 0.3, child)], self.catalog, self.cfg, MutationMemory(), "s", True)
        self.assertIsNotNone(widened)
        assert widened is not None
        self.assertEqual(widened.bound_outcome, "composition")
        self.assertGreater(widened.oracle.upper_bound, child.oracle.upper_bound)

    def test_wrappers_preserve_and_zero_bound_is_not_wrapped(self) -> None:
        spec = gen_unique_lookup(self.catalog, random.Random(6), self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        derived = propose("ast_derived", [Parent("p", 0.2, spec)], self.catalog, self.cfg, MutationMemory(), "s", True)
        cte = propose("ast_cte", [Parent("p", 0.2, spec)], self.catalog, self.cfg, MutationMemory(), "s2", True)
        assert derived is not None and cte is not None
        self.assertEqual(derived.oracle.upper_bound, 1)
        self.assertIn("WITH s0 AS", cte.sql("postgres"))
        empty = QuerySpec(
            path_kind="null_contradiction",
            select_list="*",
            from_clause="t0",
            where="c0 IS NULL",
            oracle=CardinalityOracle.exact_count(0),
            pivot_table="t0",
        )
        self.assertIsNone(propose("ast_derived", [Parent("e", 0.2, empty)], self.catalog, self.cfg, MutationMemory(), "s", True))
        self.assertIsNone(propose("ast_cte", [Parent("e", 0.2, empty)], self.catalog, self.cfg, MutationMemory(), "s", True))

    def test_subquery_union_and_null_preserve_the_key_set(self) -> None:
        spec = gen_unique_in(self.catalog, random.Random(9), self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        parent = Parent("p", 0.3, spec)
        subquery = propose("ast_in_subquery", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        exists = propose("expr_exists_corr", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        union = propose("ast_union_all", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        notnull = propose("expr_is_not_null", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        for child in (subquery, exists, union, notnull):
            self.assertIsNotNone(child)
            assert child is not None
            self.assertEqual(child.bound_outcome, "preservation")
            self.assertEqual(child.oracle.candidate_rows, spec.oracle.candidate_rows)
        assert subquery is not None and union is not None and exists is not None
        self.assertIn("SELECT", subquery.where or "")
        self.assertIn("EXISTS", exists.where or "")
        self.assertIn("UNION ALL", union.from_clause)
        any_expr = propose(
            "expr_in_to_any",
            [parent],
            self.catalog,
            self.cfg,
            MutationMemory(),
            "s",
            True,
        )
        self.assertIsNotNone(any_expr)
        assert any_expr is not None
        self.assertIn("ANY", any_expr.where or "")
        mysql = _cfg(db="mysql")
        self.assertIsNone(
            propose("expr_in_to_any", [parent], self.catalog, mysql, MutationMemory(), "s", True)
        )

    def test_design_mutations_preserve_or_compose_the_pivot(self) -> None:
        spec = gen_unique_lookup(self.catalog, random.Random(11), self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        parent = Parent("p", 0.4, spec)
        distinct = propose("ast_distinct", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        grouped = propose("ast_group_by", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        window = propose("ast_window", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        cast = propose("expr_cast_literal", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        case = propose("expr_case_eq", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        arith = propose("expr_arith_literal", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        absolute = propose("expr_abs_literal", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        taut = propose("expr_and_true", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        both = propose("ast_intersect", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        left = propose("ast_left_join", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        for child in (distinct, grouped, window, cast, case, arith, absolute, taut):
            self.assertIsNotNone(child)
            assert child is not None
            self.assertEqual(child.bound_outcome, "preservation")
            self.assertEqual(child.oracle.upper_bound, spec.oracle.upper_bound)
        assert distinct is not None and grouped is not None and window is not None
        assert cast is not None and case is not None and arith is not None and both is not None and left is not None
        self.assertIn("DISTINCT", distinct.select_list)
        self.assertEqual(grouped.group_by, spec.pivot_column or distinct.select_list.split()[-1])
        self.assertIn("ROW_NUMBER", window.select_list)
        self.assertIn("CAST(", cast.where or "")
        self.assertIn("CASE", case.where or "")
        self.assertIn("+ 0", arith.where or "")
        self.assertEqual(both.bound_outcome, "composition")
        self.assertEqual(both.oracle.upper_bound, spec.oracle.upper_bound * 2)
        self.assertIn("INTERSECT", both.from_clause)
        self.assertIn("LEFT JOIN", left.from_clause)
        self.assertEqual(len(left.occurrences), 2)
        self.assertEqual(left.occurrences[0].upper_bound, spec.oracle.upper_bound)
        self.assertFalse(left.occurrences[1].pivot)
        self.assertEqual(left.occurrences[1].upper_bound, spec.oracle.upper_bound)
        having = propose(
            "expr_having",
            [Parent("g", 0.4, grouped)],
            self.catalog,
            self.cfg,
            MutationMemory(),
            "s",
            True,
        )
        self.assertIsNotNone(having)
        assert having is not None
        self.assertEqual(having.having, grouped.where)
        self.assertEqual(having.oracle.upper_bound, grouped.oracle.upper_bound)
        all_expr = propose("expr_all_single", [parent], self.catalog, self.cfg, MutationMemory(), "s", True)
        self.assertIsNotNone(all_expr)
        assert all_expr is not None
        self.assertIn("ALL", all_expr.where or "")
        self.assertIsNone(
            propose("expr_all_single", [parent], self.catalog, _cfg(db="mysql"), MutationMemory(), "s", True)
        )

    def test_tied_parents_are_visited_round_robin(self) -> None:
        def point(table: str) -> QuerySpec:
            spec = QuerySpec(
                path_kind="unique_lookup",
                select_list="*",
                from_clause=table,
                where="c0 = 1",
                oracle=CardinalityOracle.exact_count(1, self.catalog.table(table).rows_for("c0", 1)),
                pivot_table=table,
                pivot_column="c0",
                bound_outcome="seed",
            )
            return spec

        first, second = point("t0"), point("t1")
        pool = [
            Parent(ast_fingerprint(first, "postgres"), 0.5, first),
            Parent(ast_fingerprint(second, "postgres"), 0.5, second),
        ]
        memory = MutationMemory()
        seen = []
        for _ in range(2):
            child = propose("expr_eq_to_in", pool, self.catalog, self.cfg, memory, "s", True)
            self.assertIsNotNone(child)
            assert child is not None
            seen.append(child.sql("postgres"))
        self.assertEqual(len(set(seen)), 2)

    def test_literal_projection_does_not_drop_an_aggregate_bound(self) -> None:
        spec = QuerySpec(
            path_kind="boundary_minmax",
            select_list="MIN(c0)",
            from_clause="t0",
            oracle=CardinalityOracle.exact_count(1, frozenset({1})),
            pivot_table="t0",
        )
        self.assertIsNone(
            propose("ast_literal_proj", [Parent("m", 0.2, spec)], self.catalog, self.cfg, MutationMemory(), "s", True)
        )
        filtered = gen_unique_lookup(self.catalog, random.Random(4), self.cfg)
        assert filtered is not None
        filtered.pivot_table = filtered.from_clause.strip()
        child = propose(
            "ast_literal_proj",
            [Parent("f", 0.2, filtered)],
            self.catalog,
            self.cfg,
            MutationMemory(),
            "s",
            True,
        )
        self.assertIsNotNone(child)
        assert child is not None
        self.assertEqual(child.select_list, "1 AS cpqs_lit")
        self.assertEqual(child.oracle.upper_bound, filtered.oracle.upper_bound)
        self.assertEqual(child.where, filtered.where)

    def test_unsupported_expression_wrap_is_rejected(self) -> None:
        spec = gen_unique_lookup(self.catalog, random.Random(7), self.cfg)
        assert spec is not None
        self.assertIsNone(reject_unsupported_wrap(spec))
        self.assertNotIn("expr_abs_wrap", QUERY_ARMS)

    def test_cache_hit_is_not_returned_again(self) -> None:
        spec = gen_unique_lookup(self.catalog, random.Random(8), self.cfg)
        assert spec is not None
        spec.pivot_table = spec.from_clause.strip()
        memory = MutationMemory()
        parent = Parent(ast_fingerprint(spec, "postgres"), 0.2, spec)
        first = propose("expr_eq_to_in", [parent], self.catalog, self.cfg, memory, "state-a", True)
        second = propose("expr_eq_to_in", [parent], self.catalog, self.cfg, memory, "state-a", True)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        # Tried targets belong to the episode. A new state keeps the AST cache
        # but may repeat a shape the previous state already executed.
        memory.reset_episode()
        other = propose("expr_eq_to_in", [parent], self.catalog, self.cfg, memory, "state-b", True)
        self.assertIsNotNone(other)


class StateMutationTests(unittest.TestCase):
    def test_desc_index_applies_once_and_hash_partition_is_dialect_gated(self) -> None:
        cfg = _cfg()
        profile = schema_profiles("postgres")[0]
        state = DbState(profile, 10, 200, 3, "postgres")
        desc = apply_state_mutation(state, "idx_desc", cfg)
        self.assertIsNotNone(desc)
        assert desc is not None
        self.assertIn("k", desc.profile.desc_index_on)
        self.assertIsNone(apply_state_mutation(desc, "idx_desc", cfg))
        self.assertIsNone(apply_state_mutation(state, "part_list", cfg))
        ranged = apply_state_mutation(state, "part_range", cfg)
        self.assertIsNotNone(ranged)
        assert ranged is not None
        catalog = ranged.catalog()
        self.assertTrue(catalog.primary().partitioned)

    def test_data_mutation_changes_multiplicity_without_reusing_the_bound(self) -> None:
        cfg = _cfg()
        state = DbState(schema_profiles("postgres")[0], 10, 200, 2, "postgres")
        denser = apply_state_mutation(state, "data_more_dup", cfg)
        assert denser is not None
        self.assertLess(denser.distinct_k, state.distinct_k)
        before = state.catalog().primary().multiplicity("k", 1)
        after = denser.catalog().primary().multiplicity("k", 1)
        self.assertGreater(after, before)

    def test_data_distribution_arms_change_nulls_range_and_partitions(self) -> None:
        from cpqs.gen.data import set_based_insert_sql

        cfg = _cfg()
        state = DbState(schema_profiles("postgres")[0], 10, 200, 2, "postgres")
        nulled = apply_state_mutation(state, "nulls_on", cfg)
        assert nulled is not None
        self.assertEqual(nulled.profile.null_stride, 8)
        table = nulled.catalog().primary()
        self.assertGreater(sum(1 for row in table.rows if row["payload"] is None), 0)
        self.assertIn("THEN NULL", set_based_insert_sql(table, "postgres", 10))
        denser = apply_state_mutation(nulled, "nulls_denser", cfg)
        assert denser is not None
        self.assertEqual(denser.profile.null_stride, 4)
        self.assertIsNone(apply_state_mutation(state, "nulls_clear", cfg))
        cleared = apply_state_mutation(nulled, "nulls_clear", cfg)
        assert cleared is not None
        self.assertEqual(cleared.profile.null_stride, 0)

        spread = apply_state_mutation(state, "range_spread", cfg)
        assert spread is not None
        self.assertEqual(spread.profile.k_gap, 2)
        keys = [row["k"] for row in spread.catalog().primary().rows]
        self.assertNotIn(2, keys)
        self.assertEqual(max(keys), (10 - 1) * 2 + 1)
        packed = apply_state_mutation(spread, "range_pack", cfg)
        assert packed is not None
        self.assertEqual(packed.profile.k_gap, 1)

        ranged = apply_state_mutation(state, "part_range", cfg)
        assert ranged is not None
        skewed = apply_state_mutation(ranged, "part_skew", cfg)
        assert skewed is not None
        self.assertEqual(skewed.profile.part_major_pct, 80)
        part_rows = skewed.catalog().primary().rows
        in_first = sum(1 for row in part_rows if row["part"] == 1)
        self.assertGreaterEqual(in_first, int(0.8 * len(part_rows)))
        self.assertIn("CASE WHEN", set_based_insert_sql(skewed.catalog().primary(), "postgres", 10))
        even = apply_state_mutation(skewed, "part_even", cfg)
        assert even is not None
        self.assertEqual(even.profile.part_major_pct, 0)
        self.assertIsNone(apply_state_mutation(state, "part_skew", cfg))

    def test_hash_index_is_equality_only_and_has_no_range_certificate(self) -> None:
        from cpqs.gen.schema import secondary_index_sql

        cfg = _cfg()
        state = DbState(schema_profiles("postgres")[0], 20, 400, 2, "postgres")
        hashed = apply_state_mutation(state, "idx_hash", cfg)
        self.assertIsNotNone(hashed)
        assert hashed is not None
        table = hashed.catalog().primary()
        hash_idx = next(idx for idx in table.indexes if idx.index_type == "hash")
        self.assertFalse(hash_idx.ordered)
        self.assertIsNone(table.compatible_ordered_index("payload"))
        ddl = "\n".join(secondary_index_sql(table, "postgres"))
        self.assertIn("USING HASH", ddl)
        self.assertIsNone(apply_state_mutation(DbState(schema_profiles("mysql")[0], 20, 400, 2, "mysql"), "idx_hash", _cfg(db="mysql")))

    def test_mysql_refuses_full_outer_targets(self) -> None:
        cfg = _cfg(db="mysql")
        catalog = build_catalog(100, 100, 10, n_tables=2, dialect="mysql")
        spec = gen_unique_lookup(catalog, random.Random(1), cfg)
        assert spec is not None
        spec.pivot_table = "t0"
        self.assertIsNone(propose("ast_full_outer", [Parent("p", 0.2, spec)], catalog, cfg, MutationMemory(), "s", True))


class ReportingTests(unittest.TestCase):
    def test_minmax_batch_is_not_reported_but_a_full_scan_is(self) -> None:
        spec = QuerySpec(
            path_kind="boundary_minmax",
            select_list="MIN(c0)",
            from_clause="t0",
            oracle=CardinalityOracle.exact_count(1),
        )
        cfg = Config(db="tidb", overestimate_factor=8)
        estimate = ExplainResult("EXPLAIN", "", 64, 64)
        batch = ExplainResult("EXPLAIN ANALYZE", "", 64, 64, actual_max_rows=64)
        self.assertFalse(is_violation(spec, estimate, cfg, 5000, batch))
        full_est = ExplainResult("EXPLAIN", "", 5000, 5000)
        full = ExplainResult("EXPLAIN ANALYZE", "", 5000, 5000, actual_max_rows=5000)
        self.assertTrue(is_violation(spec, full_est, cfg, 5000, full))

    def test_non_pivot_occurrence_is_checked_on_its_own_scan(self) -> None:
        spec = QuerySpec(
            path_kind="unique_in",
            select_list="*",
            from_clause="t0 INNER JOIN t1 ON t0.c0 = t1.c0",
            where="t0.c0 IN (1, 2, 3)",
            oracle=CardinalityOracle.exact_count(400),
            pivot_table="t0",
            pivot_column="c0",
            occurrences=(
                AccessOccurrence("t0", 400, "c0", True),
                AccessOccurrence("t1", 3, "c0", False),
            ),
        )
        text = (
            "Nested Loop  (actual time=0.1..2.0 rows=400 loops=1)\n"
            "  Index Scan using t0_pkey on t0  (actual time=0.1..0.4 rows=400 loops=1)\n"
            "  Seq Scan on t1  (actual time=0.2..1.5 rows=800 loops=1)\n"
        )
        plan = ExplainResult("EXPLAIN", text, 800, 800)
        executed = ExplainResult(
            "EXPLAIN ANALYZE",
            text,
            800,
            800,
            actual_max_rows=800,
            analyze_text=text,
        )
        cfg = Config(db="postgres", overestimate_factor=8)
        # 800 is inside the pivot allowance (400*8) and outside t1's allowance.
        self.assertFalse(is_violation(spec, plan, cfg, 400, executed))
        self.assertTrue(occurrence_violation(spec, plan, executed, cfg))
        self.assertEqual(examined_rows_for_table(text, "t1"), 800)
        self.assertEqual(examined_rows_for_table(text, "t0"), 400)
        self.assertEqual(max_actual_time_ms(text), 2.0)
        self.assertIsNone(max_actual_time_ms(""))


class ScheduleTests(unittest.TestCase):
    def test_episode_stalls_and_state_arm_receives_mean_reward(self) -> None:
        calls = {"eval": 0}

        def eval_fn(payload: str) -> Feedback:
            calls["eval"] += 1
            # First pull of each payload is a new plan; repeats are not.
            novel = payload.endswith("-new")
            return Feedback(d=0.1, g=1 if novel else 0, u=scalarized_reward(0.1, 1 if novel else 0), m=1)

        def mutate(_arm: str, pool) -> str | None:
            if not pool:
                return None
            return "child-old"

        def applicable(pool):
            return ["expr_eq_to_in"] if pool else []

        produced = {"n": 0}

        def next_state():
            produced["n"] += 1
            if produced["n"] > 1:
                return None
            return "idx_desc", "seed2-new"

        explorer = Explorer(("expr_eq_to_in",), ("idx_desc", "idx_expr"), stall_limit=2, pool_limit=4)
        left = explorer.explore(
            budget=20,
            seed="seed-new",
            eval_fn=eval_fn,
            mutate_fn=mutate,
            applicable_fn=applicable,
            next_state_fn=next_state,
            key_fn=lambda payload: payload,
            max_episodes=4,
            episode_cap=10,
        )
        self.assertGreaterEqual(explorer.episodes, 2)
        self.assertEqual(explorer.state_stats["idx_desc"].pulls, 1)
        self.assertGreater(explorer.state_stats["idx_desc"].mean, 0.0)
        self.assertEqual(explorer.state_stats["idx_expr"].pulls, 0)
        self.assertLess(left, 20)
        # Query statistics are reset at each episode boundary, so the second
        # episode does not inherit the first episode's pull count forever.
        self.assertLessEqual(explorer.query_stats["expr_eq_to_in"].pulls, 10)

    def test_failed_mutation_does_not_consume_the_fake_budget(self) -> None:
        evals = {"n": 0}

        def eval_fn(_payload) -> Feedback:
            evals["n"] += 1
            return Feedback(0.0, 0, 0.0, 1)

        def mutate(_arm, _pool):
            return None

        def applicable(_pool):
            return ["expr_eq_to_in"]

        explorer = Explorer(("expr_eq_to_in",), ("idx_desc",), stall_limit=3, pool_limit=4)
        left = explorer.explore(
            budget=5,
            seed="seed",
            eval_fn=eval_fn,
            mutate_fn=mutate,
            applicable_fn=applicable,
            next_state_fn=lambda: None,
            key_fn=lambda payload: payload,
            max_episodes=1,
            episode_cap=5,
        )
        self.assertEqual(evals["n"], 1)
        self.assertEqual(left, 4)
        self.assertAlmostEqual(mean_reward([0.2, 0.4]), 0.3)


if __name__ == "__main__":
    unittest.main()
