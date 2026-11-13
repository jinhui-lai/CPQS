from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional, Set

from cpqs.config import Config
from cpqs.core.catalog import Catalog
from cpqs.core.feedback import Explorer, Feedback, plan_signature, score_execution
from cpqs.core.fingerprint import bug_fingerprint, bug_kind
from cpqs.core.query import QuerySpec
from cpqs.db import get_adapter
from cpqs.db.base import Adapter, is_connection_error, is_timeout_error
from cpqs.db.explain import ExplainResult, examined_rows_for_table, max_actual_time_ms
from cpqs.gen.mutate import QUERY_ARMS, MutationMemory, ast_fingerprint, propose, seed_query
from cpqs.gen.queries import _bound_n
from cpqs.gen.schema import schema_profiles
from cpqs.gen.state_mut import STATE_ARMS, DbState, apply_state_mutation
from cpqs.poc import write_poc


@dataclass
class RunStats:
    generated: int = 0
    explained: int = 0
    skipped: int = 0
    bugs: int = 0
    states: int = 0
    mutations: int = 0
    episodes: int = 0
    pocs: List[str] = field(default_factory=list)


ANALYZE_DBS = frozenset(
    {
        "mysql",
        "mariadb",
        "postgres",
        "tidb",
        "cockroach",
        "oracle",
        "clickhouse",
        "opengauss",
        "oceanbase",
    }
)


# Emptiness proofs that must fold to Result / zero partitions: any positive
# access (even a 1-row index probe) is already a prune/exclusion miss.
_EMPTY_PROOF_KINDS = frozenset(
    {
        "partition_out_of_range",
        "partition_bound_hole",
        "partition_expr_empty",
        "check_expr_wrap",
        "check_pair_arith",
        "check_or_join_empty",
        "check_domain_baseline",
        "check_pair_point",
        "null_contradiction",
    }
)

# MIN/MAX and ordered LIMIT plans often examine one vector batch (TiDB
# coprocessor batches are 32–64) and then stop. That is not an
# orders-of-magnitude miss. The paper's reporting threshold is about 100×;
# a full scan of a populated table still clears it.
_BATCH_BOUND_KINDS = frozenset(
    {
        "boundary_minmax",
        "ordered_limit",
        "desc_ordered_limit",
        "ordered_offset",
        "prefix_order_limit",
        "tuple_gt",
    }
)


# Single-/few-partition oracles: a full Append of n_parts≈3 only over-reads ~3×,
# which sits under the default 8× overestimate factor. Use 2× so a whole-table
# scan still flags as a prune miss with the compact 3-partition schema.
_PARTITION_PRUNE_KINDS = frozenset(
    {
        "partition_eq",
        "partition_range",
        "partition_and_unique",
        "partition_expr_prune",
        "partition_list_eq",
        "partition_hash_eq",
        "partition_local_index",
    }
)


def allowed_upper(spec: QuerySpec, cfg: Config, upper: int) -> float:
    """Rows an occurrence may examine before the ratio threshold trips."""
    if bug_kind(spec.path_kind) in _EMPTY_PROOF_KINDS and upper == 0:
        return 0.0
    factor = cfg.overestimate_factor
    if bug_kind(spec.path_kind) in _PARTITION_PRUNE_KINDS:
        factor = min(factor, 2.0)
    elif bug_kind(spec.path_kind) in _BATCH_BOUND_KINDS:
        factor = max(factor, 100.0)
    return max(upper * factor, float(upper + 16))


def allowed_rows(spec: QuerySpec, cfg: Config) -> float:
    return allowed_upper(spec, cfg, spec.oracle.upper_bound)


def occurrence_violation(
    spec: QuerySpec,
    plan: ExplainResult,
    executed: Optional[ExplainResult],
    cfg: Config,
) -> bool:
    """True when a non-pivot input exceeds its own access bound.

    The reward still uses only the pivot. Other occurrences are reported
    when both the plan-wide estimate and that input's examined rows overshoot.
    """
    if plan.access_rows is None or executed is None or executed.timed_out:
        return False
    text = executed.analyze_text or executed.text or plan.text or ""
    for occ in spec.occurrences:
        if occ.pivot or occ.table == spec.pivot_table:
            continue
        actual = examined_rows_for_table(text, occ.table)
        if actual is None:
            continue
        limit = allowed_upper(spec, cfg, occ.upper_bound)
        if actual > limit and plan.access_rows > limit:
            return True
    return False


def exceeds_oracle(spec: QuerySpec, rows: Optional[int], cfg: Config) -> bool:
    if rows is None:
        return False
    return rows > allowed_rows(spec, cfg)


def _is_distinct_or_group_query(spec: QuerySpec) -> bool:
    """Result-cardinality oracles, not every query that mentions DISTINCT.

    ``loose_distinct`` and materialized-view rewrites bound the aggregate
    output. DISTINCT, GROUP BY, and window mutations keep the pivot's
    record-examination bound, so those queries stay on access counters.
    """
    return spec.path_kind in {"loose_distinct", "mv_group_rewrite"}


def executed_oracle_rows(spec: QuerySpec, executed: ExplainResult) -> Optional[int]:
    """ANALYZE rows compared against the oracle.

    Default: max actual access rows (examined), matching the access-cardinality
    oracle. DISTINCT / GROUP BY use the root operator's actual rows instead.
    """
    if _is_distinct_or_group_query(spec) and executed.actual_root_rows is not None:
        return int(math.ceil(executed.actual_root_rows))
    return executed.actual_access_rows


def is_violation(
    spec: QuerySpec,
    plan: ExplainResult,
    cfg: Config,
    n_bound: int,
    executed: Optional[ExplainResult] = None,
) -> bool:
    """True when estimated access rows exceed the oracle.

    On every supported DBMS the EXPLAIN ANALYZE (or equivalent) actual
    rows must also exceed, or ANALYZE must time out; an estimate-only
    mismatch is not a bug.
    """
    if not exceeds_oracle(spec, plan.access_rows, cfg):
        return False
    if cfg.db in ANALYZE_DBS:
        if executed is None:
            return False
        if executed.timed_out:
            return True
        return exceeds_oracle(spec, executed_oracle_rows(spec, executed), cfg)
    return True


def reporting_tau(spec: QuerySpec, cfg: Config) -> float:
    """Ratio used by the reward. Reporting itself still uses ``allowed_rows``."""
    factor = cfg.overestimate_factor
    if bug_kind(spec.path_kind) in _PARTITION_PRUNE_KINDS:
        factor = min(factor, 2.0)
    return factor


def result_within_bound(adapter: Adapter, spec: QuerySpec, cfg: Config) -> Optional[bool]:
    """True when ``COUNT(*)`` does not exceed the access upper bound.

    A performance bug still returns the right rows, so a count above the
    bound means the bound itself is wrong and must not be reported.
    ``None`` means the check did not finish. The probe projects a constant
    so engines that reject duplicate names in ``SELECT *`` joins still count.
    """
    from dataclasses import replace

    # ``SELECT *`` joins expose duplicate names to MySQL derived tables.
    # Aggregates must keep their select list so the probe counts the result.
    if spec.select_list.strip() == "*":
        query = replace(spec, select_list="1").sql(cfg.db)
    else:
        query = spec.sql(cfg.db)
    if cfg.db == "oracle":
        wrapped = f"SELECT COUNT(*) FROM ({query})"
    else:
        wrapped = f"SELECT COUNT(*) FROM ({query}) cpqs_chk"
    try:
        rows = adapter.fetchall(wrapped)
    except Exception:
        return None
    if not rows:
        return None
    return int(rows[0][0]) <= spec.oracle.upper_bound


def _materialize(adapter: Adapter, catalog: Catalog) -> None:
    try:
        adapter.setup_schema(catalog)
        adapter.load_data(catalog)
    except Exception as exc:
        print(f"schema load failed ({exc}); reconnecting")
        adapter.recover_session()
        adapter.setup_schema(catalog)
        adapter.load_data(catalog)


def run(cfg: Config) -> RunStats:
    """Query-first bandit. State mutations run only after an episode stalls.

    The query budget counts executed statements. Cached AST shapes and failed
    state mutations do not. A mutated query is reported only when the runtime
    access exceeds the bound and ``COUNT(*)`` still fits inside that bound.
    """
    rng = random.Random(cfg.seed)
    stats = RunStats()
    adapter: Optional[Adapter] = None
    if not cfg.dry_run:
        adapter = get_adapter(cfg)
        adapter.connect()
    profiles = schema_profiles(cfg.db)
    if cfg.only_kinds and all(k.startswith("partition_") for k in cfg.only_kinds):
        part_profiles = [p for p in profiles if p.partitioned]
        if part_profiles:
            profiles = part_profiles
    n0, _n1 = cfg.table_row_counts()
    distinct_k = min(cfg.distinct_k, n0)
    current = DbState(profiles[0], distinct_k, n0, cfg.n_tables, cfg.db)
    seen_states = {current.key}
    seen_fps: Set[str] = set()
    memory = MutationMemory()
    seed_cursor = [0]
    holder = {"state": current, "catalog": current.catalog()}
    explorer = Explorer(
        query_arms=QUERY_ARMS,
        state_arms=STATE_ARMS,
        stall_limit=max(1, cfg.stale_queries),
        pool_limit=32,
    )

    def state_key() -> str:
        return repr(holder["state"].key)

    def eval_query(spec: QuerySpec) -> Feedback:
        catalog = holder["catalog"]
        sql = spec.sql(cfg.db)
        tag = spec.mutation or "seed"
        stats.generated += 1
        if spec.mutation:
            stats.mutations += 1
        if cfg.dry_run:
            print(f"[{tag}|{spec.bound_outcome}|{spec.path_kind}] {spec.oracle.describe()} :: {sql}")
            # No runtime plan exists. Treat the normalized SQL shape as a
            # stand-in signature so the scheduler still mutates measurable parents.
            signature = ast_fingerprint(spec, cfg.db)
            return score_execution(0, spec.oracle.upper_bound, reporting_tau(spec, cfg), signature, explorer.plans)
        assert adapter is not None
        plan: Optional[ExplainResult] = None
        try:
            plan = adapter.explain(sql)
        except Exception as exc:
            stats.skipped += 1
            print(f"explain skipped ({spec.path_kind}/{tag}): {exc}")
            if is_connection_error(exc) or is_timeout_error(exc):
                adapter.recover_session()
            return score_execution(None, spec.oracle.upper_bound, reporting_tau(spec, cfg), None, explorer.plans)
        stats.explained += 1
        executed: Optional[ExplainResult] = None
        if adapter.supports_explain_analyze():
            try:
                executed = adapter.explain_analyze(sql)
            except Exception as exc:
                if is_timeout_error(exc):
                    adapter.recover_session()
                    executed = adapter.timed_out_result("EXPLAIN ANALYZE " + sql, exc)
                else:
                    stats.skipped += 1
                    print(f"analyze skipped ({spec.path_kind}/{tag}): {exc}")
                    if is_connection_error(exc):
                        adapter.recover_session()
                    executed = None
        timed_out = executed is not None and executed.timed_out
        actual = None if executed is None or timed_out else executed_oracle_rows(spec, executed)
        text = ""
        if executed is not None and executed.analyze_text:
            text = executed.analyze_text
        elif executed is not None and executed.text:
            text = executed.text
        elif plan is not None:
            text = plan.text
        feedback = score_execution(actual, spec.oracle.upper_bound, reporting_tau(spec, cfg), plan_signature(text), explorer.plans)
        n_bound = _bound_n(spec, catalog)
        report = plan is not None and (
            is_violation(spec, plan, cfg, n_bound, executed)
            or occurrence_violation(spec, plan, executed, cfg)
        )
        if report and plan is not None:
            elapsed = max_actual_time_ms(text)
            if elapsed is not None and elapsed < 1.0 and not timed_out:
                # A sub-millisecond plan did not pay the runtime the design
                # requires before a cardinality gap is treated as a bug.
                report = False
                print(
                    f"fast {spec.path_kind}/{tag} expected {spec.oracle.describe()} "
                    f"analyzed={actual} time_ms={elapsed:.3f}"
                )
        if report and spec.mutation and not timed_out:
            agrees = result_within_bound(adapter, spec, cfg)
            if agrees is not True:
                report = False
                print(
                    f"suppressed {spec.path_kind}/{tag} expected {spec.oracle.describe()} "
                    f"analyzed={actual} (result count does not fit the bound)"
                )
        if report and plan is not None:
            fp = bug_fingerprint(catalog, spec)
            if fp not in seen_fps:
                seen_fps.add(fp)
                stats.bugs += 1
                path = write_poc(cfg, catalog, spec, sql, plan, stats.bugs, executed=executed)
                stats.pocs.append(str(path))
                shown = "timeout" if timed_out else actual
                elapsed = max_actual_time_ms(text)
                time_note = f" time_ms={elapsed:.1f}" if elapsed is not None else ""
                lower = text.lower()
                shape = ""
                for token in ("seq scan", "table scan", "full scan", "tablescan", "partition:all"):
                    if token in lower:
                        shape = f" plan={token}"
                        break
                print(
                    f"BUG #{stats.bugs} [{holder['state'].profile.name}] {spec.path_kind}/{tag} "
                    f"expected {spec.oracle.describe()} explained={plan.access_rows} "
                    f"analyzed={shown}{time_note}{shape} d={feedback.d:.3f} g={feedback.g} -> {path}"
                )
            return feedback
        if plan is not None and exceeds_oracle(spec, plan.access_rows, cfg) and not report:
            print(
                f"estimate-only {spec.path_kind}/{tag} expected {spec.oracle.describe()} "
                f"explained={plan.access_rows} analyzed={actual} d={feedback.d:.3f}"
            )
        elif stats.explained % 25 == 0:
            print(
                f"ok {stats.explained} {spec.path_kind}/{tag} "
                f"expected {spec.oracle.describe()} analyzed={actual} "
                f"d={feedback.d:.3f} g={feedback.g} u={feedback.u:.3f}"
            )
        return feedback

    def applicable(pool) -> List[str]:
        live = []
        for arm in QUERY_ARMS:
            if propose(arm, pool, holder["catalog"], cfg, memory, state_key(), commit=False) is not None:
                live.append(arm)
        return live

    def mutate(arm: str, pool):
        spec = propose(arm, pool, holder["catalog"], cfg, memory, state_key(), commit=True)
        return spec

    def next_state():
        blocked = set()
        saved = holder["state"]
        while True:
            choices = []
            for arm in STATE_ARMS:
                if arm in blocked:
                    continue
                candidate = apply_state_mutation(saved, arm, cfg)
                if candidate is not None and candidate.key not in seen_states:
                    choices.append(arm)
            arm = explorer.select_state(choices)
            if arm is None:
                return None
            candidate = apply_state_mutation(saved, arm, cfg)
            if candidate is None or candidate.key in seen_states:
                blocked.add(arm)
                continue
            catalog = candidate.catalog()
            if adapter is not None:
                try:
                    _materialize(adapter, catalog)
                except Exception as exc:
                    print(f"state arm {arm} failed ({exc}); restoring previous state")
                    adapter.recover_session()
                    try:
                        _materialize(adapter, saved.catalog())
                    except Exception as restore_exc:
                        print(f"restore failed ({restore_exc})")
                    blocked.add(arm)
                    continue
            query = seed_query(catalog, rng, cfg, seed_cursor)
            if query is None:
                print(f"state arm {arm} produced no measurable seed; restoring")
                if adapter is not None:
                    try:
                        _materialize(adapter, saved.catalog())
                    except Exception as restore_exc:
                        print(f"restore failed ({restore_exc})")
                blocked.add(arm)
                continue
            holder["state"] = candidate
            holder["catalog"] = catalog
            seen_states.add(candidate.key)
            stats.states += 1
            memory.reset_episode()
            print(
                f"=== state {stats.states}/{cfg.max_states} arm={arm} "
                f"profile={candidate.profile.name} rows={candidate.n_rows} "
                f"distinct_k={candidate.distinct_k} ==="
            )
            return arm, query

    try:
        catalog = holder["catalog"]
        stats.states += 1
        print(
            f"=== state {stats.states}/{cfg.max_states} profile={current.profile.name} "
            f"tables={cfg.n_tables} rows={n0} distinct_k={distinct_k} "
            f"pk={current.profile.pk_type} uk={current.profile.uk_type} ==="
        )
        if adapter is not None:
            _materialize(adapter, catalog)
        first = seed_query(catalog, rng, cfg, seed_cursor)
        if first is None:
            print("initial state has no measurable seed")
            nxt = next_state()
            if nxt is not None:
                _arm, first = nxt
        explorer.explore(
            budget=cfg.queries,
            seed=first,
            eval_fn=eval_query,
            mutate_fn=mutate,
            applicable_fn=applicable,
            next_state_fn=next_state,
            key_fn=lambda spec: ast_fingerprint(spec, cfg.db),
            max_episodes=max(1, cfg.max_states),
            episode_cap=max(1, cfg.queries_per_state),
        )
        stats.episodes = explorer.episodes
        print(
            f"--- exploration finished episodes={explorer.episodes} "
            f"mutations={stats.mutations} plans={len(explorer.plans)} "
            f"last_mean={explorer.last_episode_mean:.3f}"
        )
    finally:
        if adapter is not None:
            adapter.close()
    return stats
