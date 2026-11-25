"""第三方应用账本的 PostgreSQL 集成测试（使用 JIANSHANAGENT_TEST_POSTGRES_DSN）。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qsl, urlencode
from uuid import uuid4

import asyncpg
import pytest

from jianshanagent.core.db import MIGRATIONS_DIR, Database
from jianshanagent.core.integrations import IntegrationStore
from jianshanagent.core.usage import UsageCall, UsageResult

_MIGRATION = "0012_integrations.sql"
_DAY = datetime(2026, 5, 19, tzinfo=UTC)
_NEXT = _DAY + timedelta(days=1)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    # 独立 schema；只装这一版，验证查询不依赖旧 trace，也不要求测试库有 pgvector。
    (tmp_path / _MIGRATION).symlink_to(MIGRATIONS_DIR / _MIGRATION)
    schema = f"test_integrations_{uuid4().hex}"
    address, _, query = dsn.partition("?")
    settings = dict(parse_qsl(query, keep_blank_values=True))
    settings["search_path"] = schema
    database = Database(f"{address}?{urlencode(settings)}")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await database.connect()
        assert await database.migrate(tmp_path) == [_MIGRATION]
        assert await database.migrate(tmp_path) == []
        yield database
    finally:
        await database.close()
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


def _digest(label: str) -> bytes:
    return sha256(label.encode()).digest()


async def test_apps_and_reimport_do_not_expose_or_restore_credentials(db: Database) -> None:
    store = IntegrationStore(db)
    legacy, other, rotated = map(_digest, ["legacy", "other", "rotated"])
    await store.import_legacy({legacy: "pinganzhi"})
    [app] = await store.list_apps()
    assert app == {
        "app_id": "pinganzhi",
        "name": "平安志",
        "enabled": True,
        "created_at": app["created_at"],
        "last_request_at": None,
    }
    assert isinstance(app["created_at"], datetime)
    assert await store.authenticate(legacy) == "pinganzhi"
    assert await store.authenticate(rotated) is None
    created = await store.create("other", "Other", other)
    assert created and created["enabled"] and created["app_id"] == "other"
    assert await store.create("other", "Duplicate app ID", rotated) is None
    assert await store.create("extra", "Duplicate digest", other) is None
    assert await store.rotate("pinganzhi", other) is None
    assert await store.authenticate(legacy) == "pinganzhi"
    assert await store.rotate("missing", rotated) is None
    assert await store.update("missing", enabled=False) is None

    assert await store.update("pinganzhi", name="新的名字", enabled=False) == {
        **app,
        "name": "新的名字",
        "enabled": False,
    }
    assert await store.authenticate(legacy) is None
    updated = await store.rotate("pinganzhi", rotated)
    assert updated and not updated["enabled"]
    await store.import_legacy({legacy: "pinganzhi"})
    assert await store.authenticate(legacy) is None
    assert await store.authenticate(rotated) is None
    assert await store.update("pinganzhi", enabled=True) == {**updated, "enabled": True}
    assert await store.authenticate(legacy) is None
    assert await store.authenticate(rotated) == "pinganzhi"


async def test_legacy_digest_conflicts_leave_null_and_fail_closed(db: Database) -> None:
    store = IntegrationStore(db)
    shared, first, second, new, recovered = map(
        _digest, ["shared", "first", "second", "new", "recovered"]
    )
    await store.create("existing", "Existing", shared)
    await store.import_legacy({shared: "duplicate", first: "ambiguous", second: "ambiguous"})
    for app_id in ("duplicate", "ambiguous"):
        row = await db.pool.fetchrow(
            "SELECT enabled, token_digest FROM integration_apps WHERE app_id = $1", app_id
        )
        assert row is not None and row["enabled"] is False and row["token_digest"] is None
        updated = await store.update(app_id, enabled=True)
        assert updated and updated["enabled"] is False
    assert await store.authenticate(shared) == "existing"
    assert await store.authenticate(first) is None
    assert await store.authenticate(second) is None

    await store.rotate("existing", new)
    await store.import_legacy({shared: "duplicate", first: "ambiguous"})
    assert await store.authenticate(shared) is None
    assert await store.authenticate(first) is None
    rotated = await store.rotate("duplicate", recovered)
    assert rotated and not rotated["enabled"]
    await store.update("duplicate", enabled=True)
    assert await store.authenticate(recovered) == "duplicate"


async def test_usage_scopes_retries_and_counts_by_request_date(db: Database) -> None:
    store = IntegrationStore(db)
    for app_id in ("active", "other", "unused"):
        await store.create(app_id, app_id, _digest(app_id))
    await store.update("unused", enabled=False)
    await store.record_request("active", "at-start", _DAY)
    await store.record_request("active", "later", _DAY + timedelta(hours=5))
    await store.record_request("active", "earlier", _DAY + timedelta(hours=1))
    await store.record_request("active", "at-start", _NEXT + timedelta(days=5))
    await store.record_request("other", "at-start", _NEXT + timedelta(days=5))
    apps = {row["app_id"]: row for row in await store.list_apps()}
    assert apps["active"]["last_request_at"] == _DAY + timedelta(hours=5)
    assert apps["other"]["last_request_at"] is None
    await store.record_request("active", "at-end", _NEXT)
    await store.record_request("other", "other-request", _DAY)

    # 同一个型号产生完整和异常的计量；实际开始晚于时间窗仍记在请求所属日期。
    llm = UsageCall(
        call_id="llm-1",
        request_id="at-start",
        app_id="active",
        started_at=_NEXT + timedelta(minutes=3),
        kind="llm",
        model="chat",
    )
    await store.begin_call(llm)
    await store.begin_call(llm)
    await store.finish_call(
        llm, UsageResult(prompt_tokens=8, completion_tokens=3, cached_tokens=2, reasoning_tokens=1)
    )
    await store.finish_call(llm, UsageResult(prompt_tokens=999))
    partial = replace(llm, call_id="llm-2", request_id="later")
    await store.begin_call(partial)
    await store.finish_call(partial, UsageResult(prompt_tokens=4))
    await store.begin_call(replace(llm, call_id="llm-3"))  # 异常中断，只留下尝试

    embedding = replace(llm, call_id="embedding-1", kind="embedding", model="vector")
    await store.begin_call(embedding)
    await store.finish_call(embedding, UsageResult(embedding_tokens=0))
    await store.begin_call(replace(embedding, call_id="embedding-2"))
    search = replace(llm, call_id="search-1", kind="search", model="web")
    await store.begin_call(search)
    await store.finish_call(search, UsageResult(searches=1))
    search2 = replace(search, call_id="search-2")
    await store.begin_call(search2)
    await store.finish_call(search2, UsageResult(search_tokens=7))
    excluded = replace(llm, call_id="excluded", request_id="at-end", started_at=_DAY)
    await store.begin_call(excluded)
    await store.finish_call(excluded, UsageResult(prompt_tokens=100, completion_tokens=100))
    other = replace(llm, call_id="other", request_id="other-request", app_id="other")
    await store.begin_call(other)
    await store.finish_call(other, UsageResult(prompt_tokens=44, completion_tokens=0))

    summary = await store.usage(_DAY, _NEXT, None)
    tracking_since = await db.pool.fetchval(
        "SELECT applied_at FROM schema_migrations WHERE name = $1", _MIGRATION
    )
    assert isinstance(summary["tracking_since"], datetime)
    assert summary["tracking_since"] == tracking_since
    assert [row["app_id"] for row in summary["apps"]] == ["active", "other", "unused"]
    active, other_app, unused = summary["apps"]
    assert active == {
        "app_id": "active",
        "requests": 3,
        "model_calls": 7,
        "unknown_calls": 5,
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "cached_tokens": 2,
        "reasoning_tokens": 1,
        "embedding_tokens": 0,
        "search_tokens": 7,
        "searches": 1,
        "unknown_search_tokens": 1,
        "unknown_searches": 1,
    }
    assert type(active["prompt_tokens"]) is int
    assert other_app["requests"] == other_app["model_calls"] == 1
    assert other_app["prompt_tokens"] == 44 and other_app["unknown_calls"] == 0
    assert not any(value for key, value in unused.items() if key != "app_id")
    selected = await store.usage(_DAY, _NEXT, "active")
    assert selected == {
        "tracking_since": tracking_since,
        "apps": [active],
        "models": [row for row in summary["models"] if row["app_id"] == "active"],
    }
    models = {row["kind"]: row for row in selected["models"]}
    assert (models["llm"]["calls"], models["llm"]["unknown_calls"]) == (3, 2)
    assert (models["embedding"]["calls"], models["embedding"]["unknown_calls"]) == (2, 1)
    assert (models["search"]["calls"], models["search"]["unknown_calls"]) == (2, 2)
    assert models["search"]["unknown_searches"] == models["search"]["unknown_search_tokens"] == 1
    for field in UsageResult.__dataclass_fields__:
        assert sum(row[field] for row in models.values()) == active[field]
    assert (await store.usage(_NEXT, _NEXT + timedelta(days=1), "active"))["apps"][0][
        "prompt_tokens"
    ] == 100
    assert await store.usage(_DAY, _NEXT, "nonexistent") == {
        "tracking_since": tracking_since,
        "apps": [],
        "models": [],
    }
    assert (
        await db.pool.fetchval(
            "SELECT embedding_tokens FROM integration_calls WHERE call_id = 'embedding-1'"
        )
        == 0
    )
    assert (
        await db.pool.fetchval(
            "SELECT embedding_tokens FROM integration_calls WHERE call_id = 'embedding-2'"
        )
        is None
    )


async def test_only_registered_requests_can_own_calls(db: Database) -> None:
    store = IntegrationStore(db)
    await store.create("owner", "Owner", _digest("owner"))
    await store.create("other", "Other", _digest("other"))
    await store.record_request("owner", "request", _DAY)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await store.record_request("unregistered", "new-request", _DAY)
    call = UsageCall(
        call_id="call",
        request_id="request",
        app_id="owner",
        started_at=_DAY,
        kind="llm",
        model="chat",
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await store.begin_call(replace(call, app_id="other"))
    await store.finish_call(call, UsageResult(prompt_tokens=10))  # 不能凭结束凭空生成尝试
    assert (await store.usage(_DAY, _NEXT, "owner"))["models"] == []
    await store.begin_call(call)
    await store.finish_call(replace(call, app_id="other"), UsageResult(prompt_tokens=9))
    [model] = (await store.usage(_DAY, _NEXT, "owner"))["models"]
    assert model["unknown_calls"] == 1 and model["prompt_tokens"] == 0
