"""Two-level feedback-guided exploration.

Implements the cardinality-discrepancy reward, plan-novelty bit, UCB
selection, measurable parent pool, and the query-first episode loop from
the CPQS design. Query and state mutators are injected so this module
stays independent of SQL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set


@dataclass
class Feedback:
    """One ``Eval`` result.

    ``d`` is the bounded cardinality gap, ``g`` is 1 when the normalized
    plan signature is new, ``u`` is the scalarized reward, and ``m`` is 1
    when the pivot access count is available and comparable.
    """

    d: float
    g: int
    u: float
    m: int
    signature: Optional[str] = None


@dataclass
class ArmStat:
    pulls: int = 0
    mean: float = 0.0


@dataclass
class Parent:
    """Measurable query retained as a future mutation parent."""

    key: str
    reward: float
    payload: Any


def discrepancy(actual: Optional[float], expected: float, tau: float) -> float:
    """Bounded gap ``d = C_actual / (C_actual + τ C_expected)``.

    ``d`` is 1 when the expected bound is 0 and access is confirmed
    positive, and 0 when the actual count is missing or both sides are 0.
    """
    if actual is None:
        return 0.0
    actual_f = float(actual)
    expected_f = float(expected)
    if actual_f < 0 or expected_f < 0:
        return 0.0
    denom = actual_f + max(float(tau), 0.0) * expected_f
    if denom <= 0.0:
        return 0.0
    return actual_f / denom


def scalarized_reward(d: float, g: int) -> float:
    """``u = (2/3) d + (1/3) g``. Discrepancy outweighs plan novelty."""
    return (2.0 / 3.0) * float(d) + (1.0 / 3.0) * (1 if g else 0)


def score_execution(
    actual: Optional[float],
    expected: float,
    tau: float,
    signature: Optional[str],
    seen_plans: Set[str],
) -> Feedback:
    """Compute ``d, g, u, m`` and record a new signature after ``g`` is read."""
    d = discrepancy(actual, expected, tau)
    if signature:
        novel = signature not in seen_plans
        g = 1 if novel else 0
        seen_plans.add(signature)
    else:
        g = 0
    m = 1 if actual is not None else 0
    return Feedback(d=d, g=g, u=scalarized_reward(d, g), m=m, signature=signature)


def plan_signature(text: Optional[str]) -> Optional[str]:
    """Operator and access-method skeleton with constants and estimates removed."""
    import re

    if not text:
        return None
    body = text.strip()
    if not body:
        return None
    body = body.lower()
    body = re.sub(r"\(cost=[^)]*\)", " ", body)
    body = re.sub(r"\(actual[^)]*\)", " ", body)
    body = re.sub(r"actual time=[0-9.e+-]+(?:\.\.[0-9.e+-]+)?", " ", body)
    body = re.sub(r"\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b", "#", body)
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return None
    return body[:800]


def update_arm(stat: ArmStat, reward: float) -> None:
    stat.pulls += 1
    stat.mean += (float(reward) - stat.mean) / stat.pulls


def mean_reward(rewards: Sequence[float]) -> float:
    if not rewards:
        return 0.0
    return float(sum(rewards)) / float(len(rewards))


def select_arm(stats: Dict[str, ArmStat], catalog: Sequence[str], applicable: Iterable[str]) -> Optional[str]:
    """Unpulled applicable arm first; otherwise UCB with ``sqrt(2 ln t / n)``.

    ``t`` is the next pull index over the whole catalog, not only the arms
    that happen to be applicable on this step.
    """
    live = set(applicable)
    ordered = [arm for arm in catalog if arm in live]
    if not ordered:
        return None
    t = 1 + sum(stats[arm].pulls for arm in catalog if arm in stats)
    unpulled = [arm for arm in ordered if stats[arm].pulls == 0]
    if unpulled:
        return unpulled[0]

    def ucb(arm: str) -> float:
        n = stats[arm].pulls
        return stats[arm].mean + math.sqrt((2.0 * math.log(t)) / n)

    return max(ordered, key=lambda arm: (ucb(arm), arm))


def keep_parent(
    pool: List[Parent],
    payload: Any,
    key: str,
    reward: float,
    measurable: int,
    limit: int,
) -> List[Parent]:
    """Retain a parent only when the pivot count was measurable.

    The pool stores distinct normalized AST keys and drops the lowest
    reward when it exceeds ``limit``.
    """
    if measurable != 1:
        return pool
    for index, parent in enumerate(pool):
        if parent.key == key:
            if reward > parent.reward:
                pool[index] = Parent(key, reward, payload)
            return pool
    pool.append(Parent(key, float(reward), payload))
    if len(pool) > limit:
        pool.sort(key=lambda parent: (parent.reward, parent.key))
        del pool[0]
    return pool


EvalFn = Callable[[Any], Feedback]
MutateFn = Callable[[str, List[Parent]], Optional[Any]]
ApplicableFn = Callable[[List[Parent]], List[str]]
NextStateFn = Callable[[], Optional[tuple]]
KeyFn = Callable[[Any], str]


@dataclass
class Explorer:
    """Query-first scheduler. State-arm reward is the episode mean, delayed."""

    query_arms: Sequence[str]
    state_arms: Sequence[str]
    stall_limit: int
    pool_limit: int = 32
    plans: Set[str] = field(default_factory=set)
    query_stats: Dict[str, ArmStat] = field(default_factory=dict)
    state_stats: Dict[str, ArmStat] = field(default_factory=dict)
    episodes: int = 0
    last_episode_mean: float = 0.0

    def __post_init__(self) -> None:
        self.reset_query_stats()
        self.state_stats = {arm: ArmStat() for arm in self.state_arms}

    def reset_query_stats(self) -> None:
        self.query_stats = {arm: ArmStat() for arm in self.query_arms}

    def select_query(self, applicable: Sequence[str]) -> Optional[str]:
        return select_arm(self.query_stats, self.query_arms, applicable)

    def select_state(self, applicable: Sequence[str]) -> Optional[str]:
        return select_arm(self.state_stats, self.state_arms, applicable)

    def explore(
        self,
        budget: int,
        seed: Optional[Any],
        eval_fn: EvalFn,
        mutate_fn: MutateFn,
        applicable_fn: ApplicableFn,
        next_state_fn: NextStateFn,
        key_fn: KeyFn,
        max_episodes: int,
        episode_cap: int,
    ) -> int:
        """Run episodes until the budget, the seed supply, or the episode cap ends.

        ``next_state_fn`` returns ``(state_arm, seed_payload)`` or ``None``.
        Failed state attempts stay inside that callback: they must not update
        arm statistics or consume query budget. Returns the remaining budget.
        """
        query = seed
        state_arm: Optional[str] = None
        while budget > 0 and query is not None and self.episodes < max_episodes:
            feedback = eval_fn(query)
            budget -= 1
            pool: List[Parent] = []
            pool = keep_parent(pool, query, key_fn(query), feedback.u, feedback.m, self.pool_limit)
            rewards: List[float] = [feedback.u]
            best = feedback.d
            stall = 0
            pulls = 1
            self.reset_query_stats()
            dead: Set[str] = set()
            while budget > 0 and stall < self.stall_limit and pulls < episode_cap:
                applicable = [arm for arm in applicable_fn(pool) if arm not in dead]
                arm = self.select_query(applicable)
                if arm is None:
                    break
                candidate = mutate_fn(arm, pool)
                if candidate is None:
                    dead.add(arm)
                    continue
                mutated = eval_fn(candidate)
                budget -= 1
                pulls += 1
                update_arm(self.query_stats[arm], mutated.u)
                pool = keep_parent(
                    pool, candidate, key_fn(candidate), mutated.u, mutated.m, self.pool_limit
                )
                rewards.append(mutated.u)
                if mutated.d > best or mutated.g == 1:
                    best = max(best, mutated.d)
                    stall = 0
                else:
                    stall += 1
            if state_arm is not None:
                update_arm(self.state_stats[state_arm], mean_reward(rewards))
            self.last_episode_mean = mean_reward(rewards)
            self.episodes += 1
            if budget <= 0 or self.episodes >= max_episodes:
                break
            nxt = next_state_fn()
            if nxt is None:
                break
            state_arm, query = nxt
        return budget
