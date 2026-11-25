"""PostgreSQL 第三方应用注册表和独立的、仅存计量数据的用量账本。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from asyncpg import UniqueViolationError  # type: ignore[import-untyped]

from jianshanagent.core.db import Database
from jianshanagent.core.usage import UsageCall, UsageResult

_APP_FIELDS = "app_id, name, enabled, created_at, last_request_at"
_USAGE_MIGRATION = "0012_integrations.sql"
_METRICS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "embedding_tokens",
    "search_tokens",
    "searches",
)
_SEARCH_UNKNOWN = ("unknown_search_tokens", "unknown_searches")
_METRIC_SUMS = ", ".join(f"COALESCE(SUM(c.{field}), 0) AS {field}" for field in _METRICS)


class IntegrationStore:
    def __init__(self, db: Database):
        self._db = db

    async def import_legacy(self, clients: Mapping[bytes, str]) -> None:
        """只补缺失应用；不恢复已轮换的密钥，不修改现有应用的名称和启用状态。"""
        by_app: dict[str, bytes | None] = {}
        for legacy_digest, app_id in clients.items():
            # 同一个 app ID 有多个旧摘要时，不选择任意一个来启用。
            by_app[app_id] = None if app_id in by_app else legacy_digest
        if not by_app:
            return
        async with self._db.pool.acquire() as conn, conn.transaction():
            for app_id, digest in sorted(by_app.items()):
                name = "平安志" if app_id == "pinganzhi" else app_id
                if digest is not None:
                    inserted = await conn.fetchval(
                        "INSERT INTO integration_apps (app_id, name, token_digest) "
                        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING app_id",
                        app_id,
                        name,
                        digest,
                    )
                    if inserted is not None:
                        continue
                # 旧摘要已被另一应用占用时也占住这个 app ID，但不赋凭证；须先轮换再启用。
                await conn.execute(
                    "INSERT INTO integration_apps (app_id, name, enabled) VALUES ($1, $2, false) "
                    "ON CONFLICT (app_id) DO NOTHING",
                    app_id,
                    name,
                )

    async def authenticate(self, digest: bytes) -> str | None:
        app_id: str | None = await self._db.pool.fetchval(
            "SELECT app_id FROM integration_apps WHERE token_digest = $1 AND enabled", digest
        )
        return app_id

    async def list_apps(self) -> list[dict[str, Any]]:
        rows = await self._db.pool.fetch(
            f"SELECT {_APP_FIELDS} FROM integration_apps ORDER BY app_id"
        )
        return [dict(row) for row in rows]

    async def create(self, app_id: str, name: str, digest: bytes) -> dict[str, Any] | None:
        row = await self._db.pool.fetchrow(
            "INSERT INTO integration_apps (app_id, name, token_digest) VALUES ($1, $2, $3) "
            f"ON CONFLICT DO NOTHING RETURNING {_APP_FIELDS}",
            app_id,
            name,
            digest,
        )
        return dict(row) if row is not None else None

    async def update(
        self, app_id: str, *, name: str | None = None, enabled: bool | None = None
    ) -> dict[str, Any] | None:
        row = await self._db.pool.fetchrow(
            "UPDATE integration_apps SET name = COALESCE($2, name), "
            "enabled = token_digest IS NOT NULL AND COALESCE($3, enabled) "
            f"WHERE app_id = $1 RETURNING {_APP_FIELDS}",
            app_id,
            name,
            enabled,
        )
        return dict(row) if row is not None else None

    async def rotate(self, app_id: str, digest: bytes) -> dict[str, Any] | None:
        try:
            row = await self._db.pool.fetchrow(
                f"UPDATE integration_apps SET token_digest = $2 WHERE app_id = $1 "
                f"RETURNING {_APP_FIELDS}",
                app_id,
                digest,
            )
        except UniqueViolationError:
            return None
        return dict(row) if row is not None else None

    async def record_request(self, app_id: str, id: str, started_at: datetime) -> None:
        # 同一请求的首条写入归属和时间不可变；重试既不计数也不更新最近请求时间。
        await self._db.pool.execute(
            """
            WITH added AS (
                INSERT INTO integration_requests (request_id, app_id, started_at)
                VALUES ($1, $2, $3) ON CONFLICT (request_id) DO NOTHING
                RETURNING app_id, started_at
            )
            UPDATE integration_apps AS app
            SET last_request_at = GREATEST(app.last_request_at, added.started_at)
            FROM added WHERE app.app_id = added.app_id
            """,
            id,
            app_id,
            started_at,
        )

    async def begin_call(self, call: UsageCall) -> None:
        """先写下尝试；供应商没有返回用量时，这一行本身也会被计为未知。"""
        await self._db.pool.execute(
            "INSERT INTO integration_calls (call_id, request_id, app_id, started_at, kind, model) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (call_id) DO NOTHING",
            call.call_id,
            call.request_id,
            call.app_id,
            call.started_at,
            call.kind,
            call.model,
        )

    async def finish_call(self, call: UsageCall, result: UsageResult) -> None:
        await self._db.pool.execute(
            """
            UPDATE integration_calls
            SET finished_at = now(), prompt_tokens = $4, completion_tokens = $5,
                cached_tokens = $6, reasoning_tokens = $7, embedding_tokens = $8,
                search_tokens = $9, searches = $10
            WHERE call_id = $1 AND request_id = $2 AND app_id = $3 AND finished_at IS NULL
            """,
            call.call_id,
            call.request_id,
            call.app_id,
            result.prompt_tokens,
            result.completion_tokens,
            result.cached_tokens,
            result.reasoning_tokens,
            result.embedding_tokens,
            result.search_tokens,
            result.searches,
        )

    async def usage(self, start: datetime, end: datetime, app_id: str | None) -> dict[str, Any]:
        """按原始请求时间统计 [start, end)，model_calls 包括 LLM、embedding 和搜索。

        各项用量只合计已知值；unknown_calls 报告缺少该种调用所需用量的次数。
        """
        params = (start, end) if app_id is None else (start, end, app_id)
        # 有 app 筛选时单独生成查询形状，使 `(app_id, started_at)` 可用于索引扫描。
        request_filter = "AND r.app_id = $3" if app_id is not None else ""
        app_filter = "WHERE a.app_id = $3" if app_id is not None else ""
        async with (
            self._db.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            tracking_since = await conn.fetchval(
                "SELECT applied_at FROM schema_migrations WHERE name = $1", _USAGE_MIGRATION
            )
            app_rows = await conn.fetch(
                f"""
                SELECT a.app_id, COALESCE(t.requests, 0) AS requests
                FROM integration_apps AS a
                LEFT JOIN (
                    SELECT r.app_id, COUNT(*) AS requests FROM integration_requests AS r
                    WHERE r.started_at >= $1 AND r.started_at < $2 {request_filter}
                    GROUP BY r.app_id
                ) AS t USING (app_id)
                {app_filter}
                ORDER BY a.app_id
                """,
                *params,
            )
            model_rows = await conn.fetch(
                f"""
                SELECT r.app_id, c.kind, c.model, COUNT(*) AS calls,
                    COUNT(*) FILTER (WHERE CASE c.kind
                        WHEN 'llm' THEN c.prompt_tokens IS NULL OR c.completion_tokens IS NULL
                        WHEN 'embedding' THEN c.embedding_tokens IS NULL
                        WHEN 'search' THEN c.search_tokens IS NULL OR c.searches IS NULL
                        ELSE true
                    END) AS unknown_calls,
                    COUNT(*) FILTER (WHERE c.kind = 'search' AND c.search_tokens IS NULL)
                        AS unknown_search_tokens,
                    COUNT(*) FILTER (WHERE c.kind = 'search' AND c.searches IS NULL)
                        AS unknown_searches,
                    {_METRIC_SUMS}
                FROM integration_requests AS r
                JOIN integration_calls AS c USING (app_id, request_id)
                WHERE r.started_at >= $1 AND r.started_at < $2 {request_filter}
                GROUP BY r.app_id, c.kind, c.model
                ORDER BY r.app_id, c.kind, c.model
                """,
                *params,
            )
        apps: dict[str, dict[str, Any]] = {
            row["app_id"]: dict(
                row,
                model_calls=0,
                unknown_calls=0,
                **dict.fromkeys((*_METRICS, *_SEARCH_UNKNOWN), 0),
            )
            for row in app_rows
        }
        models: list[dict[str, Any]] = []
        for row in model_rows:
            model = dict(row)
            app = apps[model["app_id"]]
            app["model_calls"] += model["calls"]
            app["unknown_calls"] += model["unknown_calls"]
            for field in (*_METRICS, *_SEARCH_UNKNOWN):
                # PostgreSQL 的 SUM(BIGINT) 是 numeric，输出保留 Python int。
                model[field] = int(model[field])
                app[field] += model[field]
            models.append(model)
        return {"tracking_since": tracking_since, "apps": list(apps.values()), "models": models}
