import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.messages import ChatScope, InboundMessage
from jianshanagent.core.plugin import EraserSpec, PluginContext
from jianshanagent.plugins.memory.plugin import MemoryPlugin
from jianshanagent.plugins.memory.store import InMemoryMemoryStore, MemoryStore, PostgresMemoryStore


@pytest.fixture
async def postgres_store() -> AsyncIterator[PostgresMemoryStore]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    db = Database(dsn)
    await db.connect()
    await db.migrate()
    try:
        yield PostgresMemoryStore(db)
    finally:
        await db.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> MemoryStore:
    if request.param == "memory":
        return InMemoryMemoryStore()
    return request.getfixturevalue("postgres_store")


async def test_store_dedupes_keeps_newest_and_isolates_scopes(store: MemoryStore) -> None:
    a, b = f"test:{uuid.uuid4()}", f"test:{uuid.uuid4()}"
    for content in ["叫我阿岚", "主力是山风", "叫我阿岚", "喜欢斗技"]:
        await store.add(a, content, keep=2)
    await store.add(b, "别人的记忆", keep=2)

    assert await store.list(a) == ["主力是山风", "喜欢斗技"]  # 重复不记，超出丢最旧
    assert await store.list(b) == ["别人的记忆"]
    assert await store.clear(a) == 2
    assert await store.list(a) == []
    assert await store.clear(b) == 1


async def test_clearing_for_a_user_reaches_his_memories_in_other_groups(
    store: MemoryStore,
) -> None:
    me, other = f"u{uuid.uuid4().hex}", f"u{uuid.uuid4().hex}"
    here, there, private = f"group:g1:user:{me}", f"group:g2:user:{me}", f"c2c:{me}"
    neighbour = f"group:g1:user:{other}"  # 同一个群里的别人
    lookalike = f"group:g1:user:x{me}"  # 标识以我的标识结尾的另一个人
    for scope in (here, there, private, neighbour, lookalike):
        await store.add(scope, "一条记忆", keep=5)

    # 不带「这个人」：只删这一个会话的（/新话题 之类按会话清的用法不变）
    assert await store.clear(private) == 1
    # 带上：他在别的群里的也删；同群的别人、标识只是长得像的人不动
    assert await store.clear(here, user_suffix=f":user:{me}") == 2
    assert await store.list(there) == []
    assert await store.list(neighbour) == ["一条记忆"]
    assert await store.list(lookalike) == ["一条记忆"]
    assert await store.clear(neighbour) == 1 and await store.clear(lookalike) == 1


def _message(scope: ChatScope, content: str = "x") -> InboundMessage:
    return InboundMessage(
        message_id="m1", chat_scope=scope, chat_id="chat", user_id="user", content=content
    )


async def test_plugin_tools_commands_and_prompt_follow_repo_config() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    config = AppConfig(root=Path("."), files={"plugin.memory": repo.plugin("memory")}, secrets={})
    ctx = PluginContext("memory", config, {})
    MemoryPlugin().register(ctx)
    service = ctx.services["memory"]
    commands = {spec.name: spec.handler for spec in ctx.commands}
    private, group = _message(ChatScope.C2C), _message(ChatScope.GROUP)

    # 单聊可以主动记偏好；群聊只记明示的
    assert "个人偏好" in await service.prompt(private)
    assert "只有用户明确要求" in await service.prompt(group)

    saved = await ctx.tools["memory_save"].handler({"content": "  叫我\n阿岚 "}, private)
    assert saved == {"saved": True}
    assert await ctx.tools["memory_save"].handler({"content": " "}, private) == {"saved": False}
    assert "关于这位用户，你记得：\n- 叫我 阿岚" in await service.prompt(private)
    assert "阿岚" not in await service.prompt(group)  # 群里和单聊的记忆分开

    assert (await commands["记忆"](private, "")).content.endswith("1. 叫我 阿岚")
    assert (await commands["清除记忆"](private, "")).content == "已删除：记忆 1 条。"
    assert (await commands["记忆"](private, "")).content == "我还没有记住关于你的任何事。"
    assert (await commands["清除记忆"](private, "")).content == "没有需要删除的数据。"

    await ctx.tools["memory_save"].handler({"content": "喜欢斗技"}, private)
    assert await ctx.tools["memory_forget_all"].handler({}, private) == {
        "deleted": {"记忆": 1},
        "failed": [],
    }

    # 别的插件存下的这个用户的数据一起删：它们登记在共用的清单里（后加载的插件登记的也算）；
    # 有一类没删成要如实告诉用户
    erased: list[str] = []

    async def erase_chatlog(message: InboundMessage) -> int:
        erased.append(message.user_id)
        return 12

    async def broken(_message: InboundMessage) -> int:
        raise RuntimeError("数据库断了")

    ctx.erasers.append(EraserSpec("群聊记录", erase_chatlog, "chatlog"))
    ctx.erasers.append(EraserSpec("别的", broken, "other"))
    await ctx.tools["memory_save"].handler({"content": "喜欢斗技"}, private)
    assert (await commands["清除记忆"](private, "")).content == (
        "已删除：记忆 1 条、群聊记录 12 条。\n别的没有删成，请稍后再发一次 /清除记忆。"
    )
    assert erased == ["user"]
    assert await ctx.tools["memory_forget_all"].handler({}, private) == {
        "deleted": {"记忆": 0, "群聊记录": 12},
        "failed": ["别的"],
    }


async def test_forget_command_in_one_group_also_clears_what_he_saved_in_another() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    config = AppConfig(root=Path("."), files={"plugin.memory": repo.plugin("memory")}, secrets={})
    ctx = PluginContext("memory", config, {})
    MemoryPlugin().register(ctx)
    commands = {spec.name: spec.handler for spec in ctx.commands}
    save = ctx.tools["memory_save"].handler

    def said(group: str, user: str) -> InboundMessage:
        return InboundMessage(
            message_id="m1", chat_scope=ChatScope.GROUP, chat_id=group, user_id=user, content="x"
        )

    await save({"content": "叫我阿岚"}, said("g1", "me"))
    await save({"content": "主力是山风"}, said("g2", "me"))
    await save({"content": "别人的事"}, said("g1", "neighbour"))

    # 平台在各个群里给同一个人的标识是同一个：在 g1 说删，g2 里记的也要删；同群的别人不动
    assert (await commands["清除记忆"](said("g1", "me"), "")).content == "已删除：记忆 2 条。"
    assert (await commands["记忆"](said("g2", "me"), "")).content == "我还没有记住关于你的任何事。"
    assert (await commands["记忆"](said("g1", "neighbour"), "")).content.endswith("1. 别人的事")
