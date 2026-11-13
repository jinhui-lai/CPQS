from __future__ import annotations

import argparse

from cpqs.config import DEFAULT_PASSWORDS, DEFAULT_PORTS, DEFAULT_USERS, MIN_ROWS, Config
from cpqs.runner import run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Expected Access Cardinality checker. "
        "Builds a controlled schema, generates index-backed queries, "
        "and records EXPLAIN mismatches as performance POCs."
    )
    p.add_argument("--db", required=True, choices=sorted(DEFAULT_PORTS))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--database", default="cpqs")
    p.add_argument("--oracle-service", default="FREEPDB1")
    p.add_argument(
        "--rows",
        type=int,
        default=MIN_ROWS,
        help=f"rows in EVERY table (minimum {MIN_ROWS} unless --allow-small-rows)",
    )
    p.add_argument(
        "--rows-t1",
        dest="rows_small",
        type=int,
        default=0,
        help="rows in t1; default is the same as --rows (also at least 10000)",
    )
    p.add_argument(
        "--tables",
        dest="n_tables",
        type=int,
        default=4,
        help="number of tables in each database state (2-4)",
    )
    p.add_argument("--queries", type=int, default=800, help="total query budget across all states")
    p.add_argument("--queries-per-state", type=int, default=400)
    p.add_argument(
        "--stale-queries",
        type=int,
        default=80,
        help="stall limit K: end a query episode after this many pulls with no larger gap and no new plan",
    )
    p.add_argument("--max-states", type=int, default=8, help="max schema rotations")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--max-cardinality-ratio", type=float, default=0.01)
    p.add_argument("--overestimate-factor", type=float, default=8.0)
    p.add_argument("--distinct-k", type=int, default=100)
    p.add_argument("--poc-dir", default="pocs")
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--only-kind", action="append", default=[])
    p.add_argument("--skip-kind", action="append", default=[])
    p.add_argument("--dry-run", action="store_true", help="generate SQL without a live DBMS")
    p.add_argument(
        "--allow-small-rows",
        action="store_true",
        help="allow --rows below 10000 (tests / debugging)",
    )
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    db = args.db
    return Config(
        db=db,
        host=args.host,
        port=args.port if args.port is not None else DEFAULT_PORTS[db],
        user=args.user if args.user is not None else DEFAULT_USERS[db],
        password=args.password if args.password is not None else DEFAULT_PASSWORDS[db],
        database=args.database,
        oracle_service=args.oracle_service,
        rows=args.rows,
        rows_small=args.rows_small,
        queries=args.queries,
        queries_per_state=args.queries_per_state,
        stale_queries=args.stale_queries,
        max_states=args.max_states,
        seed=args.seed,
        max_cardinality_ratio=args.max_cardinality_ratio,
        overestimate_factor=args.overestimate_factor,
        distinct_k=args.distinct_k,
        n_tables=max(2, min(4, args.n_tables)),
        poc_dir=args.poc_dir,
        analyze=not args.no_analyze,
        only_kinds=args.only_kind,
        skip_kinds=args.skip_kind,
        dry_run=args.dry_run,
        allow_small_rows=args.allow_small_rows,
    )


def main() -> None:
    cfg = config_from_args(build_parser().parse_args())
    stats = run(cfg)
    print(
        f"done generated={stats.generated} explained={stats.explained} "
        f"skipped={stats.skipped} bugs={stats.bugs} states={stats.states}"
    )
    if stats.bugs:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
