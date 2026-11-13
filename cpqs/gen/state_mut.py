"""Database-state mutations.

Each arm builds a new schema or data distribution from the current state.
The caller materializes it, refreshes statistics, and derives bounds again.
A no-op, a dialect that cannot build the structure, or a state that was
already explored is not a successful arm pull.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple

from cpqs.config import MIN_ROWS, Config
from cpqs.core.catalog import Catalog
from cpqs.gen.schema import (
    COVERING_INCLUDE_DBS,
    DESC_INDEX_DBS,
    EXPRESSION_INDEX_DBS,
    FK_DBS,
    GENERATED_COLUMN_DBS,
    HASH_INDEX_DBS,
    PARTIAL_INDEX_DBS,
    SchemaProfile,
    _numeric_types,
    _partition_allowed,
    build_catalog,
)

STATE_ARMS: Tuple[str, ...] = (
    "idx_desc",
    "idx_expr",
    "idx_partial",
    "idx_covering",
    "idx_dual",
    "idx_hash",
    "idx_clear_extra",
    "part_range",
    "part_list",
    "part_hash",
    "part_off",
    "part_finer",
    "unique_as_index",
    "unique_as_constraint",
    "fk_on",
    "mv_on",
    "pk_composite_ab",
    "extra_unique_ab",
    "type_bigint",
    "type_decimal",
    "data_more_dup",
    "data_fewer_dup",
    "rows_double",
    "rows_half",
    "nulls_on",
    "nulls_denser",
    "nulls_clear",
    "range_spread",
    "range_pack",
    "part_skew",
    "part_even",
)


@dataclass(frozen=True)
class DbState:
    profile: SchemaProfile
    distinct_k: int
    n_rows: int
    n_tables: int
    dialect: str

    @property
    def key(self) -> tuple:
        profile = self.profile
        return (
            profile.pk_type,
            profile.uk_type,
            profile.k_type,
            profile.unique_as,
            profile.composite_ab,
            profile.pk_keys,
            profile.extra_unique,
            profile.extra_unique_as,
            profile.partitioned,
            profile.n_parts,
            profile.partition_kind,
            profile.desc_index_on,
            profile.expression_abs_k,
            profile.partial_on_k,
            profile.covering_include,
            profile.with_fk,
            profile.dual_secondary,
            profile.materialized_by_k,
            profile.hash_payload,
            profile.k_gap,
            profile.null_stride,
            profile.part_major_pct,
            self.distinct_k,
            self.n_rows,
            self.n_tables,
            self.dialect,
        )

    def catalog(self) -> Catalog:
        return build_catalog(
            self.n_rows,
            self.n_rows,
            self.distinct_k,
            self.profile,
            self.dialect,
            self.n_tables,
        )


def _retarget(state: DbState, arm: str, profile: SchemaProfile, **changes: object) -> Optional[DbState]:
    updated = DbState(
        profile=replace(profile, name=f"{profile.name}+{arm}"[:80]),
        distinct_k=int(changes.get("distinct_k", state.distinct_k)),
        n_rows=int(changes.get("n_rows", state.n_rows)),
        n_tables=state.n_tables,
        dialect=state.dialect,
    )
    if updated.distinct_k < 1 or updated.distinct_k > updated.n_rows:
        return None
    if updated.key == state.key:
        return None
    return updated


def apply_state_mutation(state: DbState, arm: str, cfg: Config) -> Optional[DbState]:
    """Return the mutated state, or ``None`` when the arm does not apply."""
    profile = state.profile
    dialect = state.dialect
    if arm == "idx_desc":
        if dialect not in DESC_INDEX_DBS or "k" in profile.desc_index_on:
            return None
        return _retarget(state, arm, replace(profile, desc_index_on=profile.desc_index_on + ("k",)))
    if arm == "idx_expr":
        if dialect not in EXPRESSION_INDEX_DBS and dialect not in GENERATED_COLUMN_DBS:
            return None
        if profile.expression_abs_k:
            return None
        return _retarget(state, arm, replace(profile, expression_abs_k=True))
    if arm == "idx_partial":
        if dialect not in PARTIAL_INDEX_DBS or profile.partial_on_k or profile.partitioned:
            return None
        return _retarget(state, arm, replace(profile, partial_on_k=True))
    if arm == "idx_covering":
        if dialect not in COVERING_INCLUDE_DBS or profile.covering_include:
            return None
        return _retarget(state, arm, replace(profile, covering_include=True))
    if arm == "idx_dual":
        if profile.dual_secondary:
            return None
        return _retarget(state, arm, replace(profile, dual_secondary=True, composite_ab=False))
    if arm == "idx_hash":
        if dialect not in HASH_INDEX_DBS or profile.hash_payload:
            return None
        return _retarget(state, arm, replace(profile, hash_payload=True))
    if arm == "idx_clear_extra":
        if not (
            profile.desc_index_on
            or profile.expression_abs_k
            or profile.partial_on_k
            or profile.covering_include
            or profile.dual_secondary
            or profile.hash_payload
        ):
            return None
        return _retarget(
            state,
            arm,
            replace(
                profile,
                desc_index_on=(),
                expression_abs_k=False,
                partial_on_k=False,
                covering_include=False,
                dual_secondary=False,
                composite_ab=True,
                hash_payload=False,
            ),
        )
    if arm in {"part_range", "part_list", "part_hash"}:
        kind = arm.split("_", 1)[1]
        if not _partition_allowed(kind, dialect):
            return None
        if profile.partitioned and profile.partition_kind == kind:
            return None
        return _retarget(
            state,
            arm,
            replace(profile, partitioned=True, partition_kind=kind, n_parts=3, partial_on_k=False),
        )
    if arm == "part_off":
        if not profile.partitioned:
            return None
        return _retarget(state, arm, replace(profile, partitioned=False))
    if arm == "part_finer":
        if not profile.partitioned or profile.n_parts >= 5:
            return None
        if not _partition_allowed(profile.partition_kind, dialect):
            return None
        return _retarget(state, arm, replace(profile, n_parts=profile.n_parts + 1))
    if arm == "unique_as_index":
        if profile.unique_as == "unique_index":
            return None
        return _retarget(state, arm, replace(profile, unique_as="unique_index"))
    if arm == "unique_as_constraint":
        if profile.unique_as == "constraint":
            return None
        return _retarget(state, arm, replace(profile, unique_as="constraint"))
    if arm == "fk_on":
        if dialect not in FK_DBS or profile.with_fk:
            return None
        return _retarget(state, arm, replace(profile, with_fk=True))
    if arm == "mv_on":
        if dialect != "oracle" or profile.materialized_by_k:
            return None
        return _retarget(state, arm, replace(profile, materialized_by_k=True))
    if arm == "pk_composite_ab":
        if profile.pk_keys == ("a", "b"):
            return None
        return _retarget(state, arm, replace(profile, pk_keys=("a", "b")))
    if arm == "extra_unique_ab":
        if ("a", "b") in profile.extra_unique:
            return None
        return _retarget(state, arm, replace(profile, extra_unique=profile.extra_unique + (("a", "b"),)))
    if arm == "type_bigint":
        _int_t, big_t, _dec_t = _numeric_types(dialect)
        if profile.pk_type == big_t and profile.uk_type == big_t:
            return None
        return _retarget(state, arm, replace(profile, pk_type=big_t, uk_type=big_t))
    if arm == "type_decimal":
        _int_t, _big_t, dec_t = _numeric_types(dialect)
        if profile.pk_type == dec_t:
            return None
        return _retarget(state, arm, replace(profile, pk_type=dec_t, uk_type=dec_t))
    if arm == "data_more_dup":
        if state.distinct_k <= 2:
            return None
        return _retarget(state, arm, profile, distinct_k=max(2, state.distinct_k // 2))
    if arm == "data_fewer_dup":
        grown = min(state.n_rows, state.distinct_k * 2)
        if grown <= state.distinct_k:
            return None
        return _retarget(state, arm, profile, distinct_k=grown)
    if arm == "rows_double":
        if state.n_rows >= 100000:
            return None
        return _retarget(state, arm, profile, n_rows=state.n_rows * 2)
    if arm == "rows_half":
        floor = 100 if cfg.allow_small_rows else MIN_ROWS
        halved = state.n_rows // 2
        if halved < floor or halved < state.distinct_k:
            return None
        return _retarget(state, arm, profile, n_rows=halved)
    if arm == "nulls_on":
        if profile.null_stride:
            return None
        return _retarget(state, arm, replace(profile, null_stride=8))
    if arm == "nulls_denser":
        if profile.null_stride <= 2:
            return None
        return _retarget(state, arm, replace(profile, null_stride=max(2, profile.null_stride // 2)))
    if arm == "nulls_clear":
        if not profile.null_stride:
            return None
        return _retarget(state, arm, replace(profile, null_stride=0))
    if arm == "range_spread":
        gap = profile.k_gap * 2
        max_k = (state.distinct_k - 1) * gap + 1
        # Keep every k <= some primary key in the table so the value still exists.
        if gap > 16 or max_k > state.n_rows:
            return None
        return _retarget(state, arm, replace(profile, k_gap=gap))
    if arm == "range_pack":
        if profile.k_gap <= 1:
            return None
        return _retarget(state, arm, replace(profile, k_gap=1))
    if arm == "part_skew":
        if (
            not profile.partitioned
            or profile.n_parts < 2
            or profile.part_major_pct >= 80
            or not _partition_allowed(profile.partition_kind, dialect)
        ):
            return None
        return _retarget(state, arm, replace(profile, part_major_pct=80))
    if arm == "part_even":
        if profile.part_major_pct <= 0:
            return None
        return _retarget(state, arm, replace(profile, part_major_pct=0))
    return None
