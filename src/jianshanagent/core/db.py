"""Postgres 连接池与迁移。

迁移脚本放在 ``jianshanagent/migrations/*.sql``，按文件名顺序执行，
已执行的记在 ``schema_migrations`` 表里，每个脚本只跑一次。
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class Database:
    def __init__(self, dsn: str, *, pool_min: int = 1, pool_max: int = 10):
        if not dsn:
            raise ValueError(
                "缺少 Postgres DSN（JIANSHANAGENT_POSTGRES_DSN 或 secrets.yaml 的 postgres.dsn）"
            )
        self._dsn = dsn
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("数据库尚未连接，请先调用 connect()")
        return self._pool

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._pool_min, max_size=self._pool_max
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def migrate(self, directory: Path = MIGRATIONS_DIR) -> list[str]:
        """执行尚未应用的迁移，返回本次应用的脚本名。"""
        applied_now: list[str] = []
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES ($1)", path.name
                    )
                applied_now.append(path.name)
                logger.info("migration_applied", name=path.name)
        return applied_now
