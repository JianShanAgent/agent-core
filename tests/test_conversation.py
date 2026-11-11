import os
import uuid
from collections.abc import AsyncIterator

import pytest

from jianshanagent.core.conversation import (
    ConversationStore,
    MemoryConversationStore,
    PostgresConversationStore,
    select_recent_turns,
    strip_media,
)
from jianshanagent.core.db import Database

TOOL_TURN = [
    {"role": "user", "content": "山风是什么稀有度"},
    {
        "role": "assistant",
        "content": "",
        "reasoning_content": "先查山风",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "wiki_get", "arguments": "{}"},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "稀有度 SSR"},
    {"role": "assistant", "content": "山风是 SSR", "reasoning_content": "整理答案"},
]


def test_strip_media_replaces_parts_with_placeholders() -> None:
    message = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "https://multimedia.nt.qq.com.cn/x?rkey=1"}},
            {"type": "text", "text": "这是谁"},
        ],
    }
    assert strip_media(message) == {"role": "user", "content": "[图片]这是谁"}
    assert strip_media({"role": "user", "content": "纯文本"}) == {
        "role": "user",
        "content": "纯文本",
    }


def _turn(n: int) -> list[dict]:
    return [{"role": "user", "content": f"问{n}"}, {"role": "assistant", "content": f"答{n}"}]


def _stamped(*turns: tuple[float, list[dict]]) -> list[tuple[float, dict]]:
    return [(at, message) for at, messages in turns for message in messages]


def test_select_drops_leading_partial_turn() -> None:
    cut = _stamped((0, TOOL_TURN[2:]), (1, _turn(2)))
    assert select_recent_turns(cut, now=2) == _turn(2)
    assert select_recent_turns(_stamped((0, TOOL_TURN)), now=1) == TOOL_TURN
    assert select_recent_turns(_stamped((0, TOOL_TURN[1:])), now=1) == []


def test_select_keeps_only_the_latest_turns_whole() -> None:
    entries = _stamped((0, TOOL_TURN), *((n, _turn(n)) for n in range(1, 6)))
    # 6 轮里取最近 5 轮：最早那轮连同它的工具调用整轮丢掉
    assert select_recent_turns(entries, now=6, max_turns=5) == [
        m for n in range(1, 6) for m in _turn(n)
    ]
    assert select_recent_turns(entries, now=6, max_turns=1) == _turn(5)


def test_select_stops_at_idle_gap() -> None:
    hour = 3600.0
    entries = _stamped((0, _turn(1)), (1 * hour, _turn(2)), (4 * hour, _turn(3)))
    # 最新一轮在 2 小时内：带上它；它和上一轮隔了 3 小时，更早的都不带
    assert select_recent_turns(entries, now=5 * hour, idle_seconds=2 * hour) == _turn(3)
    # 距最新一轮也超过 2 小时：当作新对话
    assert select_recent_turns(entries, now=6.5 * hour, idle_seconds=2 * hour) == []
    # 每个间隔都不超过 2 小时就一直连着，哪怕整段对话超过 2 小时
    chained = _stamped(*((n * 1.5 * hour, _turn(n)) for n in range(4)))
    assert len(select_recent_turns(chained, now=5 * hour, idle_seconds=2 * hour)) == 8
    # 0 表示不按时间过期
    assert len(select_recent_turns(entries, now=99 * hour, idle_seconds=0)) == 6


async def test_memory_store_starts_fresh_after_idle() -> None:
    now = [0.0]
    store = MemoryConversationStore(clock=lambda: now[0])
    await store.append("k", _turn(1))
    now[0] = 7000
    assert await store.history("k") == _turn(1)
    now[0] = 7300  # 超过默认的 2 小时
    assert await store.history("k") == []
    await store.append("k", _turn(2))
    now[0] = 7400
    assert await store.history("k") == _turn(2)  # 新对话开始后，旧的一轮不会回来


@pytest.fixture
async def postgres_store() -> AsyncIterator[PostgresConversationStore]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    db = Database(dsn)
    await db.connect()
    await db.migrate()
    try:
        yield PostgresConversationStore(db)
    finally:
        await db.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> ConversationStore:
    if request.param == "memory":
        return MemoryConversationStore()
    return request.getfixturevalue("postgres_store")


async def test_roundtrip_preserves_reasoning_and_tool_calls(store: ConversationStore) -> None:
    key = f"test:{uuid.uuid4()}"
    await store.append(key, TOOL_TURN)

    assert await store.history(key) == TOOL_TURN
    assert await store.history(f"test:{uuid.uuid4()}") == []
    assert await store.clear(key) == len(TOOL_TURN)
    assert await store.history(key) == []


async def test_clearing_for_a_user_reaches_his_history_in_other_groups(
    store: ConversationStore,
) -> None:
    me, other = f"u{uuid.uuid4().hex}", f"u{uuid.uuid4().hex}"
    here, there = f"group:g1:user:{me}", f"group:g2:user:{me}"
    neighbour, lookalike = f"group:g1:user:{other}", f"group:g1:user:x{me}"
    for key in (here, there, neighbour, lookalike):
        await store.append(key, _turn(1))

    assert await store.clear(here, user_suffix=f":user:{me}") == 2 * len(_turn(1))
    assert await store.history(there) == []  # 他在另一个群里的上文也没了
    assert await store.history(neighbour) == _turn(1)  # 同群的别人不动
    assert await store.history(lookalike) == _turn(1)  # 标识只是以他的结尾的人不动
    await store.clear(neighbour)
    await store.clear(lookalike)


async def test_history_is_limited_by_whole_turns(store: ConversationStore) -> None:
    key = f"test:{uuid.uuid4()}"
    await store.append(key, TOOL_TURN)
    for n in range(1, 4):
        await store.append(key, _turn(n))

    # 按轮取：带工具调用的那一轮要么整轮带上，要么整轮不带
    assert await store.history(key, max_turns=2) == _turn(2) + _turn(3)
    assert await store.history(key, max_turns=3) == _turn(1) + _turn(2) + _turn(3)
    assert await store.history(key, max_turns=4) == TOOL_TURN + _turn(1) + _turn(2) + _turn(3)
    assert await store.history(key, max_turns=9) == TOOL_TURN + _turn(1) + _turn(2) + _turn(3)
    await store.clear(key)


async def test_media_is_not_persisted(store: ConversationStore) -> None:
    key = f"test:{uuid.uuid4()}"
    await store.append(
        key,
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://qq.example/expiring"}},
                    {"type": "text", "text": "这是谁"},
                ],
            }
        ],
    )
    assert await store.history(key) == [{"role": "user", "content": "[图片]这是谁"}]
    await store.clear(key)


async def test_migrate_is_idempotent(postgres_store: PostgresConversationStore) -> None:
    assert await postgres_store._db.migrate() == []
