from types import SimpleNamespace

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import load_config
from jianshanagent.core.conversation import MemoryConversationStore
from jianshanagent.core.messages import ChatScope, InboundMessage
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.chat.plugin import ChatPlugin


def _message(user_id: str) -> InboundMessage:
    return InboundMessage(
        message_id="m1", chat_scope=ChatScope.GROUP, chat_id="g1", user_id=user_id, content="x"
    )


async def test_reset_command_clears_only_the_callers_history() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    store = MemoryConversationStore()
    llm = SimpleNamespace(settings=SimpleNamespace(history_max_turns=5, history_idle_seconds=7200))
    ctx = PluginContext("chat", repo, {"llm": llm, "conversation_store": store})
    ChatPlugin().register(ctx)
    commands = {spec.name: spec.handler for spec in ctx.commands}

    mine, other = _message("u1"), _message("u2")
    turn = [{"role": "user", "content": "山风是谁"}, {"role": "assistant", "content": "SSR 式神"}]
    await store.append(mine.session_key, turn)
    await store.append(other.session_key, turn)

    reply = await commands["新话题"](mine, "")

    assert reply is not None and "重新开始" in reply.content
    assert await store.history(mine.session_key) == []
    assert await store.history(other.session_key) == turn  # 同群其他人的上下文不受影响
