from __future__ import annotations

from cpqs.config import Config
from cpqs.db.base import Adapter


def get_adapter(cfg: Config) -> Adapter:
    name = cfg.db.lower()
    if name == "mysql":
        from cpqs.db.mysql import MySQLAdapter

        return MySQLAdapter(cfg)
    if name == "mariadb":
        from cpqs.db.mysql import MariaDBAdapter

        return MariaDBAdapter(cfg)
    if name == "tidb":
        from cpqs.db.mysql import TiDBAdapter

        return TiDBAdapter(cfg)
    if name == "oceanbase":
        from cpqs.db.mysql import OceanBaseAdapter

        return OceanBaseAdapter(cfg)
    if name == "postgres":
        from cpqs.db.postgres import PostgresAdapter

        return PostgresAdapter(cfg)
    if name == "cockroach":
        from cpqs.db.postgres import CockroachAdapter

        return CockroachAdapter(cfg)
    if name == "opengauss":
        from cpqs.db.postgres import OpenGaussAdapter

        return OpenGaussAdapter(cfg)
    if name == "oracle":
        from cpqs.db.oracle import OracleAdapter

        return OracleAdapter(cfg)
    if name == "clickhouse":
        from cpqs.db.clickhouse import ClickHouseAdapter

        return ClickHouseAdapter(cfg)
    raise ValueError(f"unsupported database: {cfg.db}")
