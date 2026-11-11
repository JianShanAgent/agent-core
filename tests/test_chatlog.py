import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.messages import ChatScope, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext, erase_user_data
from jianshanagent.plugins.chatlog.plugin import ChatLog, ChatLogPlugin
from jianshanagent.plugins.chatlog.store import (
    ChatEntry,
    ChatLogStore,
    InMemoryChatLogStore,
    PostgresChatLogStore,
)


def _message(content: str = "大家早上好", **changes: object) -> InboundMessage:
    fields: dict = {
        "message_id": f"test-{uuid.uuid4()}",
        "chat_scope": ChatScope.GROUP,
        "chat_id": "GROUP",
        "user_id": "MEMBER",
        "content": content,
        "timestamp": "2026-07-21T08:00:00+08:00",
        "user_name": "小明",
        "addressed": False,
        "raw": {
            "content": content,
            "mentions": [{"id": "OTHER", "username": "阿岚"}],
            "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_1", "auth_token=abc"]},
            "msg_elements": [
                {"message_scene": {"ext": ["auth_token=def", "ref_msg_idx=REFIDX_0"]}}
            ],
        },
    }
    return InboundMessage(**{**fields, **changes})


def test_entry_keeps_the_event_but_not_its_tokens_and_reads_the_time_it_was_sent() -> None:
    entry = ChatEntry.of(_message())
    assert (entry.scope, entry.chat_id, entry.user_id, entry.user_name) == (
        "group",
        "GROUP",
        "MEMBER",
        "小明",
    )
    assert (entry.content, entry.addressed) == ("大家早上好", False)
    assert entry.sent_at == datetime(2026, 7, 21, 8, tzinfo=timezone(timedelta(hours=8)))
    # 事件原文留着（被 @ 的人、引用了哪条），一次性的鉴权令牌去掉，嵌套的消息里的也去掉
    assert entry.raw["mentions"] == [{"id": "OTHER", "username": "阿岚"}]
    assert entry.raw["message_scene"]["ext"] == ["msg_idx=REFIDX_1"]
    assert entry.raw["msg_elements"][0]["message_scene"]["ext"] == ["ref_msg_idx=REFIDX_0"]

    # 平台没给时间：用收到的时间
    late = ChatEntry.of(_message(timestamp=""))
    assert abs(datetime.now(UTC) - late.sent_at) < timedelta(seconds=5)


@pytest.fixture
async def postgres_store() -> AsyncIterator[PostgresChatLogStore]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    db = Database(dsn)
    await db.connect()
    await db.migrate()
    try:
        yield PostgresChatLogStore(db)
    finally:
        await db.pool.execute(
            "DELETE FROM chat_messages "
            "WHERE message_id LIKE 'test-%' OR message_id LIKE 'reply:test-%'"
        )
        await db.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> ChatLogStore:
    if request.param == "memory":
        return InMemoryChatLogStore()
    return request.getfixturevalue("postgres_store")


async def test_store_records_each_message_once(store: ChatLogStore) -> None:
    entry = ChatEntry.of(_message("<@OTHER> 早上好"))
    assert await store.add(entry) is True
    assert await store.add(entry) is False  # 断线续接时平台重发的同一条：不重复记
    assert await store.add(ChatEntry.of(_message("第二条", user_name=None))) is True

    if isinstance(store, PostgresChatLogStore):
        row = await store._db.pool.fetchrow(
            "SELECT scope, chat_id, user_name, content, addressed, sent_at, raw->'mentions' AS m "
            "FROM chat_messages WHERE message_id = $1",
            entry.message_id,
        )
        assert (row["scope"], row["chat_id"], row["user_name"]) == ("group", "GROUP", "小明")
        assert (row["content"], row["addressed"]) == ("<@OTHER> 早上好", False)
        assert row["sent_at"] == entry.sent_at and "阿岚" in row["m"]


async def _rows(store: ChatLogStore, *ids: str) -> dict[str, dict]:
    """这几条记录现在还在不在、原文还在不在（两种存储都能查）。"""
    if isinstance(store, InMemoryChatLogStore):
        return {
            key: {"raw": entry.raw, "content": entry.content}
            for key, entry in store.entries.items()
            if key in ids
        }
    assert isinstance(store, PostgresChatLogStore)
    rows = await store._db.pool.fetch(
        "SELECT message_id, content, raw::text AS raw FROM chat_messages "
        "WHERE message_id = ANY($1::text[])",
        list(ids),
    )
    return {
        row["message_id"]: {"raw": json.loads(row["raw"]), "content": row["content"]}
        for row in rows
    }


async def test_store_forgets_one_person_and_what_was_said_to_them(store: ChatLogStore) -> None:
    me, other = f"ME-{uuid.uuid4().hex}", f"OTHER-{uuid.uuid4().hex}"
    asked = _message("超鬼王是什么", user_id=me, addressed=True, raw={"author": {"id": me}})
    chatter = _message("今天抽到了", user_id=me, raw={})
    answer = ChatEntry.reply(asked, OutboundMessage(content="超鬼王是一个活动", markdown=True))
    # 别人的发言：一条 @ 了我（事件原文里有我的标识），一条和我无关；还有机器人给别人的回复
    mention = _message(
        "@小明 来打超鬼王", user_id=other, raw={"mentions": [{"id": me, "username": "小明"}]}
    )
    unrelated = _message("早", user_id=other, raw={"author": {"id": other}})
    their_answer = ChatEntry.reply(unrelated, OutboundMessage(content="早上好"))
    for entry in (
        ChatEntry.of(asked),
        ChatEntry.of(chatter),
        answer,
        ChatEntry.of(mention),
        ChatEntry.of(unrelated),
        their_answer,
    ):
        assert await store.add(entry) is True
    assert (answer.from_bot, answer.reply_to, answer.user_id) == (True, asked.message_id, "bot")
    assert answer.message_id == f"reply:{asked.message_id}" and answer.raw["markdown"] is True

    assert await store.forget("") == 0  # 空的标识什么都不删
    assert await store.forget(me) == 2  # 我的两条发言
    left = await _rows(
        store,
        asked.message_id,
        chatter.message_id,
        answer.message_id,
        mention.message_id,
        unrelated.message_id,
        their_answer.message_id,
    )
    # 我的发言、机器人给我的回复都没了；别人的发言还在，但提到我的那条事件原文清空了
    assert set(left) == {mention.message_id, unrelated.message_id, their_answer.message_id}
    assert left[mention.message_id] == {"raw": {}, "content": "@小明 来打超鬼王"}
    assert left[unrelated.message_id]["raw"] == {"author": {"id": other}}
    assert await store.forget(me) == 0


async def test_store_prunes_what_is_older_than_the_cutoff(store: ChatLogStore) -> None:
    old = _message("很久以前", timestamp="2000-01-01T00:00:00+08:00")
    new = _message("刚才")
    await store.add(ChatEntry.of(old))
    await store.add(ChatEntry.of(new))
    assert await store.prune(datetime(2001, 1, 1, tzinfo=UTC)) == 1
    assert set(await _rows(store, old.message_id, new.message_id)) == {new.message_id}


async def test_plugin_listens_to_group_messages_only_and_needs_a_database() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    assert "chatlog" in repo.get("core.plugins")["enabled"]
    config = AppConfig(root=Path("."), files={"plugin.chatlog": repo.plugin("chatlog")}, secrets={})

    ctx = PluginContext("chatlog", config, {})
    ChatLogPlugin().register(ctx)
    assert ctx.observers == [] and ctx.erasers == []  # 没有数据库：不生效

    store = InMemoryChatLogStore()
    ctx = PluginContext("chatlog", config, {})
    ChatLogPlugin(store).register(ctx)
    (observe,) = ctx.observers
    (observe_reply,) = ctx.reply_observers

    await observe(_message("群里的闲聊", timestamp=""))
    asked = _message("@了机器人", addressed=True, timestamp="")
    await observe(asked)
    await observe(_message("单聊的话", chat_scope=ChatScope.C2C, timestamp=""))
    # 机器人的最终回复也记；单聊里的不记
    await observe_reply(asked, OutboundMessage(content="答复"))
    await observe_reply(_message("单聊", chat_scope=ChatScope.C2C), OutboundMessage(content="x"))
    assert [(e.content, e.addressed, e.from_bot) for e in store.entries.values()] == [
        ("群里的闲聊", False, False),
        ("@了机器人", True, False),
        ("答复", False, True),
    ]

    # 用户要求删除他的数据时：登记在共用的清单里，由 /清除记忆 调用（见 test_memory.py）
    assert [spec.label for spec in ctx.erasers] == ["群聊记录"]
    deleted, failed = await erase_user_data(ctx.erasers, asked)
    assert (deleted, failed) == ({"群聊记录": 2}, []) and store.entries == {}


async def test_plugin_drops_records_older_than_keep_days_about_once_an_hour() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    keep_days = repo.plugin("chatlog")["keep_days"]
    assert keep_days > 0  # 仓库里的配置是会过期清理的
    config = AppConfig(root=Path("."), files={"plugin.chatlog": repo.plugin("chatlog")}, secrets={})
    store = InMemoryChatLogStore()
    ctx = PluginContext("chatlog", config, {})
    ChatLogPlugin(store).register(ctx)
    (observe,) = ctx.observers

    def aged(days: float) -> InboundMessage:
        moment = datetime.now(UTC) - timedelta(days=days)
        return _message(f"{days} 天前", timestamp=moment.isoformat())

    await store.add(ChatEntry.of(aged(keep_days + 1)))
    await store.add(ChatEntry.of(aged(keep_days - 1)))
    await observe(_message("新消息", timestamp=""))  # 记消息时顺手清理
    assert [e.content for e in store.entries.values()] == [f"{keep_days - 1} 天前", "新消息"]

    # 刚清过：一小时内不再清
    await store.add(ChatEntry.of(aged(keep_days + 2)))
    await observe(_message("又一条", timestamp=""))
    assert len(store.entries) == 4

    # keep_days 为 0：一直留着
    log = ChatLog(InMemoryChatLogStore(), scopes={"group"}, keep_days=0)
    await log.store.add(ChatEntry.of(aged(9999)))
    await log.record(_message("新消息", timestamp=""))
    assert len(log.store.entries) == 2  # type: ignore[attr-defined]
