"""管理后台查数据库的那些语句，和处理记录的落库、清理。要真的 Postgres：
设了 ``JIANSHANAGENT_TEST_POSTGRES_DSN`` 才跑（和别的 Postgres 测试一样）。

统计是对整张表算的，所以每次测试用一个独一无二的来源名（``channel``），只统计自己插的那几行。
"""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jianshanagent.admin.queries import (
    CHAT_DAYS,
    SESSION_DAYS,
    AdminQueries,
    fill_series,
    split_session,
)
from jianshanagent.core.conversation import PostgresConversationStore
from jianshanagent.core.db import Database
from jianshanagent.core.messages import ChatScope, InboundMessage
from jianshanagent.core.trace import LLMCall, Trace
from jianshanagent.core.trace_store import PostgresTraceStore
from jianshanagent.plugins.chatlog.store import ChatEntry, PostgresChatLogStore
from jianshanagent.plugins.knowledge.image_store import KbImage, PostgresImageStore
from jianshanagent.plugins.knowledge.store import KbChunk, PostgresKnowledgeStore
from jianshanagent.plugins.knowledge.sync import PostgresStatusStore
from jianshanagent.plugins.memory.store import PostgresMemoryStore

FP = "test:fake:4096"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    database = Database(dsn)
    await database.connect()
    await database.migrate()
    try:
        yield database
    finally:
        pool = database.pool
        await pool.execute("DELETE FROM llm_calls WHERE trace_id LIKE 'test-%' OR profile = 'test'")
        await pool.execute("DELETE FROM traces WHERE id LIKE 'test-%'")
        await pool.execute("DELETE FROM llm_messages WHERE session_key LIKE '%:test-%'")
        await pool.execute("DELETE FROM memories WHERE scope LIKE '%:test-%'")
        await pool.execute("DELETE FROM chat_messages WHERE message_id LIKE 'test-%'")
        await pool.execute("DELETE FROM admin_labels WHERE id LIKE 'test-%'")
        await database.close()


def _trace(
    channel: str,
    question: str,
    *,
    route: str,
    seconds: float,
    status: str = "replied",
    outcome: str = "",
    ago: timedelta = timedelta(minutes=5),
    user: str = "test-u1",
    scope: ChatScope = ChatScope.C2C,
    **summary: Any,
) -> Trace:
    chat_id = user if scope == ChatScope.C2C else "test-g1"
    message = InboundMessage(
        message_id=f"test-{uuid.uuid4().hex}",
        chat_scope=scope,
        chat_id=chat_id,
        user_id=user,
        content=question,
        user_name="阿岚",
        channel=channel,
    )
    record = Trace(message)
    record.id = f"test-{uuid.uuid4().hex}"
    record.started_at = datetime.now(UTC) - ago
    record.route, record.status, record.outcome = route, status, outcome
    record.seconds, record.reply = seconds, f"回答：{question}"
    record.summary = dict(summary)
    return record


def _call(record: Trace | None, purpose: str, profile: str, **fields: Any) -> LLMCall:
    call = LLMCall(
        started_at=record.started_at if record else datetime.now(UTC),
        seconds=fields.pop("seconds", 2.0),
        profile=profile,
        model="mimo-test",
        thinking=profile in {"reason", "review"},
        ok=fields.pop("ok", True),
        finish_reason="stop",
        prompt_tokens=1000,
        completion_tokens=100,
        reasoning_tokens=60,
        purpose=purpose,
        trace_id=record.id if record else None,
        request={"messages": [{"role": "user", "content": "提示词"}], "tools": []},
        response={"content": "回复", "reasoning": "", "tool_calls": []},
        **fields,
    )
    if record is not None:
        record.calls.append(call)
    return call


async def test_overview_traces_and_calls(db: Database) -> None:
    store, queries = PostgresTraceStore(db), AdminQueries(db)
    channel = f"t{uuid.uuid4().hex[:8]}"
    answered = _trace(
        channel, "千姬的 CV 是谁", route="research", seconds=12.5, outcome="answered",
        research={"topic": "式神", "rejections": 0, "stages": ["analyze", "kb", "review"]},
    )  # fmt: skip
    rejected = _trace(
        channel, "今年超鬼王有哪些", route="research", seconds=70.0, outcome="not_found",
        ago=timedelta(minutes=4), research={"topic": "活动", "rejections": 2},
    )  # fmt: skip
    command = _trace(channel, "/查 山风", route="command", seconds=0.2, ago=timedelta(minutes=3))
    failed = _trace(
        channel, "你好", route="chat", seconds=1.0, status="error", ago=timedelta(minutes=2),
        user="test-u2", scope=ChatScope.GROUP,
    )  # fmt: skip
    old = _trace(channel, "三天前的", route="chat", seconds=1.0, ago=timedelta(days=3))
    calls = [
        _call(answered, "analyst", "reason", seconds=4.0),
        _call(answered, "review", "review", seconds=6.0),
        _call(rejected, "analyst", "reason", seconds=30.0, ok=False, error="等不及了"),
        _call(failed, "entry", "chat", seconds=1.0),
    ]
    for record in (answered, rejected, command, failed, old):
        await store.save_trace(record)
    await store.save_trace(answered)  # 同一条再存一次不会重复
    for call in calls:
        await store.save_call(call)

    since = datetime.now(UTC) - timedelta(hours=24)
    stats = await queries.overview(since, channel=channel, daily=False)
    totals = stats["totals"]
    assert (totals["messages"], totals["replied"], totals["errors"]) == (4, 3, 1)
    assert (totals["research"], totals["answered"], totals["not_found"]) == (2, 1, 1)
    assert (totals["commands"], totals["chitchat"], totals["users"], totals["groups"]) == (
        1,
        1,
        2,
        1,
    )
    assert totals["research_median_seconds"] == pytest.approx(41.25)
    assert (totals["llm_calls"], totals["llm_failed"]) == (4, 1)
    assert (totals["prompt_tokens"], totals["reasoning_tokens"]) == (4000, 240)
    # 用时分布（只算查证）：<5、5–10、10–20、20–30、30–60、60–120、≥120
    assert stats["durations"] == [0, 0, 1, 0, 0, 1, 0]
    assert {row["route"]: row["count"] for row in stats["routes"]} == {
        "research": 2,
        "command": 1,
        "chat": 1,
    }
    assert {row["topic"]: row["count"] for row in stats["topics"]} == {"式神": 1, "活动": 1}
    profiles = {row["profile"]: row for row in stats["profiles"]}
    assert (profiles["reason"]["calls"], profiles["reason"]["failed"]) == (2, 1)
    assert profiles["reason"]["avg_seconds"] == pytest.approx(17.0)
    assert {row["purpose"]: row["calls"] for row in stats["purposes"]} == {
        "analyst": 2,
        "review": 1,
        "entry": 1,
    }
    # 补成连续的：没有消息的时段也占一格
    now = datetime.now()
    series = fill_series(
        now - timedelta(hours=24),
        now,
        daily=False,
        traces=stats["trace_series"],
        calls=stats["call_series"],
    )
    assert len(series) in (24, 25) and sum(point["messages"] for point in series) <= 4
    assert sum(point["llm_calls"] for point in series) <= 4

    # 列表：筛选、翻页
    async def ids(**filters: Any) -> list[str]:
        return [row["id"] for row in await queries.traces(limit=50, channel=channel, **filters)]

    assert await ids() == [failed.id, command.id, rejected.id, answered.id, old.id]
    assert await ids(route="research") == [rejected.id, answered.id]
    assert await ids(outcome="answered") == [answered.id]
    assert await ids(status="error") == [failed.id]
    assert await ids(search="超鬼王") == [rejected.id]
    assert await ids(min_seconds=10) == [rejected.id, answered.id]
    assert await ids(problems_only=True) == [failed.id, rejected.id]
    assert await ids(session_key="group:test-g1:user:test-u2") == [failed.id]
    assert await ids(before=command.started_at) == [rejected.id, answered.id, old.id]
    row = (await queries.traces(limit=1, channel=channel, route="research"))[0]
    assert row["summary"]["research"]["rejections"] == 2 and row["question"] == "今年超鬼王有哪些"

    detail = await queries.trace(answered.id)
    assert detail is not None and detail["finished"] is True and detail["steps"] == []
    assert [(c["purpose"], c["profile"], c["has_detail"]) for c in detail["calls"]] == [
        ("analyst", "reason", True),
        ("review", "review", True),
    ]
    assert "request" not in detail["calls"][0]  # 列表里不带提示词
    full = await queries.call(detail["calls"][0]["id"])
    assert full is not None and full["request"]["messages"][0]["content"] == "提示词"
    assert await queries.trace("test-nope") is None and await queries.call(-1) is None

    listed = await queries.calls(limit=10, profile="reason")
    mine = [c for c in listed if c["trace_id"] in {answered.id, rejected.id}]
    assert [c["ok"] for c in mine] == [False, True]
    assert all(not c["ok"] for c in await queries.calls(limit=5, failed_only=True))
    usage = await queries.profile_usage(since)
    assert usage["reason"]["calls"] >= 2

    # 清理：提示词过期清空（数字留着）；处理记录过期连同它的调用一起删
    await db.pool.execute(
        "UPDATE llm_calls SET started_at = now() - interval '20 days' WHERE trace_id = $1",
        answered.id,
    )
    cleared, removed = await store.prune(prompt_days=14, trace_days=0)
    assert cleared >= 2 and removed == 0
    stale = await queries.trace(answered.id)
    assert stale is not None and [c["has_detail"] for c in stale["calls"]] == [False, False]
    assert stale["calls"][0]["prompt_tokens"] == 1000
    _, removed = await store.prune(prompt_days=0, trace_days=2)
    assert removed >= 1 and await queries.trace(old.id) is None
    assert await queries.trace(command.id) is not None

    # 用户要求删除他的数据：他的处理记录连同模型调用一起删——在别的群里问的也删；别人的不动
    elsewhere = _trace(
        channel, "在另一个群里问的", route="chat", seconds=1.0, user="test-u2",
        scope=ChatScope.GROUP,
    )  # fmt: skip
    elsewhere.chat_id, elsewhere.session_key = "test-g2", "group:test-g2:user:test-u2"
    await store.save_trace(elsewhere)
    assert await store.erase_user(failed.session_key, "test-u2") == 2
    assert await queries.trace(failed.id) is None and await queries.trace(elsewhere.id) is None
    assert not [c for c in await queries.calls(limit=100) if c["trace_id"] == failed.id]
    assert await queries.trace(rejected.id) is not None
    assert await store.erase_user(failed.session_key, "test-u2") == 0

    # 提出删除要求的那条消息自己的记录：按编号删，名下已经存了的模型调用一起删
    await store.erase_trace(rejected.id)
    assert await queries.trace(rejected.id) is None
    assert not [c for c in await queries.calls(limit=100) if c["trace_id"] == rejected.id]
    assert await queries.trace(command.id) is not None


async def test_sessions_memories_chatlog_and_labels(db: Database) -> None:
    queries = AdminQueries(db)
    tag = uuid.uuid4().hex[:8]
    user, group = f"test-u{tag}", f"test-g{tag}"
    solo, member = f"c2c:{user}", f"group:{group}:user:{user}"
    conversations, memories = PostgresConversationStore(db), PostgresMemoryStore(db)
    await conversations.append(
        solo,
        [
            {"role": "user", "content": "山风技能"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "要查",
                "tool_calls": [{"id": "c1", "function": {"name": "research", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "assistant", "content": "山风的技能是……"},
        ],
    )
    await memories.add(solo, "叫我阿岚", keep=50)
    await memories.add(member, "主力是山风", keep=50)
    await PostgresTraceStore(db).save_trace(
        _trace("qq", "山风技能", route="research", seconds=9.0, user=user, outcome="answered")
    )

    found = await queries.sessions(limit=50, offset=0, search=tag)
    rows = {row["session_key"]: row for row in found["items"]}
    assert found["total"] == 2 and set(rows) == {solo, member}
    assert (rows[solo]["messages"], rows[solo]["turns"], rows[solo]["memories"]) == (4, 1, 1)
    assert (rows[solo]["traces"], rows[solo]["user_name"], rows[solo]["scope"]) == (
        1,
        "阿岚",
        "c2c",
    )
    assert (rows[member]["scope"], rows[member]["chat_id"], rows[member]["user_id"]) == (
        "group",
        group,
        user,
    )
    assert split_session(solo) == {"scope": "c2c", "chat_id": user, "user_id": ""}
    assert split_session("guild:频道1:user:甲") == {
        "scope": "guild",
        "chat_id": "频道1",
        "user_id": "甲",
    }

    detail = await queries.session(solo)
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert detail["messages"][1]["message"]["tool_calls"][0]["function"]["name"] == "research"
    assert [m["content"] for m in detail["memories"]] == ["叫我阿岚"]
    assert await queries.delete_memory(detail["memories"][0]["id"]) == 1
    assert await queries.clear_memories(member) == 1
    assert await queries.clear_history(solo) == 4
    assert (await queries.session(solo))["total_messages"] == 0

    # 群聊记录
    chatlog = PostgresChatLogStore(db)
    base = datetime.now(UTC) - timedelta(minutes=10)
    for number, (text, addressed, who) in enumerate(
        [("早上好", False, "甲"), ("山风怎么配御魂", True, "乙"), ("谢谢", False, "乙")]
    ):
        await chatlog.add(
            ChatEntry(
                message_id=f"test-{tag}-{number}",
                scope="group",
                chat_id=group,
                user_id=f"test-m{tag}-{who}",
                user_name=who,
                content=text,
                addressed=addressed,
                sent_at=base + timedelta(minutes=number),
                raw={"attachments": [{"url": "x"}] if number == 1 else []},
            )
        )
    chat = next(row for row in await queries.chats() if row["chat_id"] == group)
    assert (chat["messages"], chat["addressed"], chat["members"], chat["last_day"]) == (3, 1, 2, 3)
    newest_first = await queries.chat_messages(group, limit=10)
    assert [m["content"] for m in newest_first] == ["谢谢", "山风怎么配御魂", "早上好"]
    assert newest_first[1]["attachments"] == 1 and newest_first[1]["addressed"] is True
    assert [m["content"] for m in await queries.chat_messages(group, limit=10, search="御魂")] == [
        "山风怎么配御魂"
    ]
    only = await queries.chat_messages(group, limit=10, addressed_only=True)
    assert len(only) == 1
    older = await queries.chat_messages(group, limit=10, before=newest_first[0]["id"])
    assert [m["content"] for m in older] == ["山风怎么配御魂", "早上好"]
    assert await queries.user_names([f"test-m{tag}-乙", "nobody"]) == {f"test-m{tag}-乙": "乙"}

    # 备注名
    await queries.set_label("chat", group, "测试群")
    await queries.set_label("user", user, "阿岚本人")
    await queries.set_label("chat", group, "测试群（改名）")
    labels = await queries.labels()
    assert labels["chat"][group] == "测试群（改名）" and labels["user"][user] == "阿岚本人"
    await queries.set_label("chat", group, "")
    assert group not in (await queries.labels())["chat"]

    assert any(row["name"] == "traces" for row in await queries.tables())


async def test_session_and_chat_lists_only_read_a_recent_window(db: Database) -> None:
    """会话清单、群聊清单默认只看最近一段时间的行（页面开着就会反复来取，不能每次扫整张表）。"""
    queries, pool = AdminQueries(db), db.pool
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)
    fresh, back, stale = (f"c2c:test-u{tag}-{name}" for name in ("fresh", "back", "stale"))
    nickname = f"昵称{tag}"

    async def history(key: str, ago: timedelta, count: int) -> None:
        for number in range(count):
            await pool.execute(
                "INSERT INTO llm_messages (session_key, role, message, created_at) "
                "VALUES ($1, $2, '{}'::jsonb, $3)",
                key,
                "assistant" if number % 2 else "user",
                now - ago,
            )

    async def memory(key: str, ago: timedelta) -> None:
        await pool.execute(
            "INSERT INTO memories (scope, content, created_at) VALUES ($1, '记一条', $2)",
            key,
            now - ago,
        )

    async def handled(key: str, ago: timedelta) -> None:
        record = _trace("qq", "问一句", route="chat", seconds=1.0, ago=ago, user=key[4:])
        record.user_name = nickname
        await PostgresTraceStore(db).save_trace(record)

    # fresh：一小时前聊过。back：四十天前聊过三句，两天前又记了一条记忆。stale：只有四十天前的
    await history(fresh, timedelta(hours=1), 2)
    await handled(fresh, timedelta(hours=1))
    await history(back, timedelta(days=40), 3)
    await memory(back, timedelta(days=2))
    await history(stale, timedelta(days=40), 2)
    await memory(stale, timedelta(days=40))
    await handled(stale, timedelta(days=40))

    async def keys(**kwargs: Any) -> list[str]:
        found = await queries.sessions(limit=50, offset=0, **{"search": tag, **kwargs})
        assert found["total"] == len(found["items"])
        return [row["session_key"] for row in found["items"]]

    # 默认只列最近 30 天有动静的，最近的在前
    assert SESSION_DAYS == 30 and await keys() == [fresh, back]
    assert await keys(days=1) == [fresh]
    assert await keys(days=7) == [fresh, back]
    assert await keys(days=0) == [fresh, back, stale]  # 0 = 不限时间

    rows = {
        row["session_key"]: row
        for row in (await queries.sessions(limit=50, offset=0, search=tag))["items"]
    }
    # 条数是这个会话全部的（包括窗口外的那三句），最后活动时间是那条记忆的
    assert (rows[back]["messages"], rows[back]["turns"], rows[back]["memories"]) == (3, 2, 1)
    assert (rows[back]["traces"], rows[back]["user_name"], rows[back]["channel"]) == (0, None, None)
    assert abs(datetime.fromisoformat(rows[back]["last_at"]) - (now - timedelta(days=2))) < (
        timedelta(seconds=5)
    )
    assert (rows[fresh]["traces"], rows[fresh]["user_name"], rows[fresh]["channel"]) == (
        1,
        nickname,
        "qq",
    )
    everything = await queries.sessions(limit=50, offset=0, search=tag, days=0)
    old = next(row for row in everything["items"] if row["session_key"] == stale)
    assert (old["messages"], old["turns"], old["memories"], old["traces"]) == (2, 1, 1, 1)
    assert old["user_name"] == nickname and old["scope"] == "c2c"

    # 翻页：总数跟着这一页一起出来；翻过头了这一页是空的，总数照样对
    first = await queries.sessions(limit=2, offset=0, search=tag, days=0)
    assert first["total"] == 3 and [r["session_key"] for r in first["items"]] == [fresh, back]
    last = await queries.sessions(limit=2, offset=2, search=tag, days=0)
    assert last["total"] == 3 and [r["session_key"] for r in last["items"]] == [stale]
    beyond = await queries.sessions(limit=2, offset=10, search=tag, days=0)
    assert beyond == {"total": 3, "items": []}
    assert await queries.sessions(limit=2, offset=0, search=f"没有{tag}") == {
        "total": 0,
        "items": [],
    }

    # 按昵称搜：只认这段时间里处理记录上出现过的；按备注名搜也行
    assert await keys(search=nickname) == [fresh]
    assert await keys(search=nickname, days=0) == [fresh, stale]
    await queries.set_label("chat", back[4:], f"备注{tag}")
    assert await keys(search=f"备注{tag}") == [back]
    await queries.set_label("chat", back[4:], "")

    # 按时间找对话历史里最近的行要有索引，不然还是整张表读一遍
    assert await pool.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'llm_messages_created_idx'"
    )

    # ---- 群聊清单：默认最近 7 天，条数、人数也只算这几天的 ----
    chatlog = PostgresChatLogStore(db)
    busy, quiet = f"test-g{tag}-busy", f"test-g{tag}-quiet"
    said = [
        (busy, "甲", timedelta(hours=1), True),
        (busy, "乙", timedelta(hours=2), False),
        (busy, "丙", timedelta(days=3), False),
        (busy, "丁", timedelta(days=20), True),
        (busy, "甲", timedelta(days=20, hours=1), False),
        (quiet, "甲", timedelta(days=20), False),
    ]
    for number, (chat_id, who, ago, addressed) in enumerate(said):
        await chatlog.add(
            ChatEntry(
                message_id=f"test-{tag}-w{number}",
                scope="group",
                chat_id=chat_id,
                user_id=f"test-m{tag}-{who}",
                user_name=who,
                content="说了一句",
                addressed=addressed,
                sent_at=now - ago,
                raw={},
            )
        )

    async def groups(**kwargs: Any) -> dict[str, dict[str, Any]]:
        listed = [row for row in await queries.chats(**kwargs) if tag in row["chat_id"]]
        assert [row["last_at"] for row in listed] == sorted(
            (row["last_at"] for row in listed), reverse=True
        )
        return {row["chat_id"]: row for row in listed}

    def numbers(row: dict[str, Any]) -> tuple[int, int, int, int]:
        return row["messages"], row["addressed"], row["members"], row["last_day"]

    week = await groups()
    assert CHAT_DAYS == 7 and set(week) == {busy}  # 这几天没人说话的群不列
    assert numbers(week[busy]) == (3, 1, 3, 2) and week[busy]["scope"] == "group"
    assert abs(datetime.fromisoformat(week[busy]["first_at"]) - (now - timedelta(days=3))) < (
        timedelta(seconds=5)
    )
    assert numbers((await groups(days=1))[busy]) == (2, 1, 2, 2)
    month = await groups(days=30)
    assert numbers(month[busy]) == (5, 2, 4, 2) and numbers(month[quiet]) == (1, 0, 1, 0)
    assert await groups(days=0) == month  # 0 = 不限时间


async def test_knowledge_browsing(db: Database) -> None:
    queries = AdminQueries(db)
    chunks = [
        KbChunk(
            chunk_id=f"c{number}",
            doc_key="page:千姬" if number < 3 else "dashen:1",
            kind="entry" if number < 3 else "announcement",
            title="千姬" if number < 3 else "更新公告",
            text=f"千姬 > 档案\n第 {number} 片：CV 高垣彩阳" if number < 3 else "100%_满勤 奖励",
            url="https://x.test/千姬",
            section_path=("档案",),
            seq=number,
            date="2024-01-01" if number == 3 else None,
        )
        for number in range(4)
    ]
    vector = [1.0] + [0.0] * 4095
    knowledge = PostgresKnowledgeStore(db)
    images = PostgresImageStore(db)
    try:
        await knowledge.sync(
            [(chunk, vector if chunk.seq != 2 else None) for chunk in chunks], fingerprint=FP
        )
        await images.sync(
            [
                (
                    KbImage(image_id="Skin_1_01.png", kind="式神立绘", label="千姬的初始立绘"),
                    vector,
                ),
                (KbImage(image_id="Tx_1_s.png", kind="式神头像", label="千姬的头像"), None),
            ],
            fingerprint=FP,
            preprocess="p",
        )
        summary = await queries.kb_summary()
        types = {row["type"]: row for row in summary["types"]}
        assert (
            types["entry"]["chunks"],
            types["entry"]["docs"],
            types["entry"]["with_vectors"],
        ) == (
            3,
            1,
            2,
        )
        assert {row["kind"]: row["images"] for row in summary["image_kinds"]} == {
            "式神立绘": 1,
            "式神头像": 1,
        }

        # 自动更新的状态：没跑过是 None；跑过就是它写下的那一份（对账用的那一项不给页面）
        assert await queries.kb_sync() is None
        status = PostgresStatusStore(db)
        await status.write({"checked_at": "2026-11-11T22:20:11+0800", "loaded": {"kb": "某一版"}})
        try:
            assert await queries.kb_sync() == {"checked_at": "2026-11-11T22:20:11+0800"}
        finally:
            await db.pool.execute("DELETE FROM kb_meta WHERE key = 'sync'")

        page = await queries.kb_chunks(limit=2, offset=0, kind="entry", search=None)
        assert page["total"] == 3 and [row["chunk_id"] for row in page["items"]] == ["c0", "c1"]
        assert (
            page["items"][0]["preview"].startswith("千姬 > 档案") and page["items"][0]["chars"] > 0
        )
        found = await queries.kb_chunks(limit=10, offset=0, kind=None, search="高垣")
        assert found["total"] == 3
        # 搜索词里的 % 和 _ 是普通字符，不是通配符
        literal = await queries.kb_chunks(limit=10, offset=0, kind=None, search="100%_满")
        assert [row["chunk_id"] for row in literal["items"]] == ["c3"]
        assert (await queries.kb_chunks(limit=10, offset=0, kind=None, search="1%片"))["total"] == 0

        chunk = await queries.kb_chunk("c1")
        assert chunk is not None and chunk["has_vector"] is True and chunk["fingerprint"] == FP
        assert [row["chunk_id"] for row in chunk["siblings"]] == ["c0", "c1", "c2"]
        assert (await queries.kb_chunk("c2"))["has_vector"] is False  # type: ignore[index]
        assert await queries.kb_chunk("nope") is None

        gallery = await queries.kb_images(limit=10, offset=0, kind=None, search="头像")
        assert [row["image_id"] for row in gallery["items"]] == ["Tx_1_s.png"]
        assert gallery["items"][0]["has_vector"] is False
        assert (await queries.kb_images(limit=10, offset=0, kind="式神立绘", search=None))[
            "total"
        ] == 1
    finally:
        await knowledge.sync([], fingerprint=FP)  # 测试库用完清空
        await images.sync([], fingerprint=FP, preprocess="p")


async def test_backup_state_reads_what_the_backup_container_recorded(db: Database) -> None:
    queries = AdminQueries(db)
    pool = db.pool
    # 备份容器写的行（deploy/backup/backup.sh 的 record）。
    # 时间放在很远的将来，不和测试库里别的行搅在一起
    insert = (
        "INSERT INTO backup_runs (finished_at, ok, file, bytes, copied, detail) "
        "VALUES ($1, $2, $3, $4, $5, $6)"
    )
    good, bad = datetime(2100, 1, 1, tzinfo=UTC), datetime(2100, 1, 2, tzinfo=UTC)
    try:
        await pool.execute(insert, good, True, "jianshanagent-21000101-033000.dump", 1234, True, "")
        state = await queries.backup_state()
        assert state["last"] == {
            "finished_at": good,
            "ok": True,
            "file": "jianshanagent-21000101-033000.dump",
            "bytes": 1234,
            "copied": True,
            "detail": "",
        }
        assert state["last_ok_at"] == good
        # 从什么时候开始有这份记录：建这张表的那次数据库变更
        applied = await pool.fetchval(
            "SELECT applied_at FROM schema_migrations WHERE name LIKE '%backup%'"
        )
        assert applied is not None and state["since"] == applied

        # 后面一次失败了：最近一次是失败的，最近一次成功还是前面那次
        await pool.execute(insert, bad, False, "", 0, False, "test-pg_dump 没有成功")
        state = await queries.backup_state()
        assert state["last"]["ok"] is False and state["last"]["detail"].startswith("test-")
        assert state["last"]["finished_at"] == bad and state["last_ok_at"] == good
    finally:
        await pool.execute("DELETE FROM backup_runs WHERE finished_at >= $1", good)
