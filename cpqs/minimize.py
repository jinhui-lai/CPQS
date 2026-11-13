"""Minimize CPQS POC files. Live dual-validation, greedy reductions.

Usage:
  python3 -m cpqs.minimize --db mysql --poc-dir pocs
  python3 -m cpqs.minimize --db mysql --file pocs/mysql/bug-0001-tuple_gt.sql
  python3 -m cpqs.minimize --db mysql --offline   # unused tables only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cpqs.config import DEFAULT_PASSWORDS, DEFAULT_PORTS, DEFAULT_USERS, Config
from cpqs.db import get_adapter
from cpqs.minpoc import drop_unused_tables, minimize_poc, parse_poc, render_poc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Minimize CPQS cardinality POCs.")
    p.add_argument("--db", required=True, choices=sorted(DEFAULT_PORTS))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--database", default="cpqs")
    p.add_argument("--oracle-service", default="FREEPDB1")
    p.add_argument("--poc-dir", default="pocs")
    p.add_argument("--file", action="append", default=[], help="specific POC path (repeatable)")
    p.add_argument("--overestimate-factor", type=float, default=8.0)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--offline", action="store_true", help="only drop tables the query never names")
    p.add_argument("--dry-run", action="store_true", help="print reductions, do not write files")
    p.add_argument("--no-analyze", action="store_true")
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
        overestimate_factor=args.overestimate_factor,
        timeout_s=args.timeout,
        poc_dir=args.poc_dir,
        analyze=not args.no_analyze,
    )


def poc_paths(args: argparse.Namespace) -> list[Path]:
    if args.file:
        return [Path(p) for p in args.file]
    root = Path(args.poc_dir) / args.db
    return sorted(p for p in root.glob("bug-*.sql"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    paths = poc_paths(args)
    if not paths:
        print(f"no POCs for {cfg.db}", file=sys.stderr)
        return 1
    adapter = None
    if not args.offline:
        adapter = get_adapter(cfg)
        adapter.connect()
        try:
            adapter.prepare_database()
        except Exception as exc:
            print(f"prepare_database: {exc}", file=sys.stderr)
    wrote = 0
    skipped = 0
    try:
        for path in paths:
            print(f"== {path}")
            poc = parse_poc(path)
            if args.offline:
                nxt = drop_unused_tables(poc)
                if nxt is None:
                    print("  already minimal (offline tables)")
                    skipped += 1
                    continue
                text = render_poc(nxt, cfg, None, None)
                if args.dry_run:
                    print(text)
                else:
                    path.write_text(text, encoding="utf-8")
                    print(f"  wrote unused-table shrink ({nxt.notes[-1]})")
                wrote += 1
                continue
            assert adapter is not None
            minimized, plan, executed, status = minimize_poc(poc, adapter, cfg)
            if status != "ok":
                print(f"  skip: {status}")
                skipped += 1
                continue
            text = render_poc(minimized, cfg, plan, executed)
            if args.dry_run:
                print("  would write:")
                print(text[:500])
            else:
                path.write_text(text, encoding="utf-8")
                extra = f" reductions={len(minimized.notes)}" if minimized.notes else " (plan refresh)"
                print(f"  wrote {path}{extra}")
            wrote += 1
    finally:
        if adapter is not None:
            adapter.close()
    print(f"done wrote={wrote} skipped={skipped} total={len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
