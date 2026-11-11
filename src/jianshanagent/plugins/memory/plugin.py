"""记忆插件：让对话记住用户说过的事。

- 写：模型通过 ``memory_save`` 工具记。群聊里只记用户明确要求记住的；
  单聊里还可以主动记长期有用的偏好（由 ``plugin.memory`` 的 scope 控制）。
- 读：chat 插件每次对话前调用 :meth:`MemoryService.prompt`，把记忆写进系统提示。
- 删：``/清除记忆`` 指令，或用户用自然语言要求时模型调用 ``memory_forget_all``。
  平台运营规范要求用户提出时必须能删除其数据，所以这两个入口删的不只是记忆：
  别的插件存下的这个用户的数据（比如群聊记录）也一起删——各插件用 ``ctx.add_eraser`` 登记怎么删，
  这里挨个调用（见 core/plugin.py 的 ``erase_user_data``）。
"""

from __future__ import annotations

from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import ChatScope, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext, erase_user_data
from jianshanagent.plugins.memory.store import InMemoryMemoryStore, MemoryStore, PostgresMemoryStore

logger = get_logger(__name__)

#: 「以后还用得上的个人偏好」举的例子。因站而异（阴阳师：常用的式神和阵容、游戏进度），
#: 配置里的 ``preference_examples`` 可以换
DEFAULT_PREFERENCE_EXAMPLES = "怎么称呼、常问的内容、习惯的说法"
_WHEN_TO_SAVE = {
    "explicit_only": "只有用户明确要求你记住某件事时，才调用 memory_save。",
    "preferences_ok": (
        "用户明确要求你记住某件事，或透露了以后还用得上的个人偏好"
        "（{examples}）时，调用 memory_save。"
    ),
}


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        max_items: int,
        max_chars: int,
        scope_cfg: dict,
        preference_examples: str = DEFAULT_PREFERENCE_EXAMPLES,
    ):
        self.store = store
        self.preference_examples = preference_examples
        self.max_items = max_items
        self.max_chars = max_chars
        self._policy = {
            ChatScope.GROUP: str(scope_cfg.get("group") or "explicit_only"),
            ChatScope.C2C: str(scope_cfg.get("c2c") or "preferences_ok"),
        }

    async def save(self, message: InboundMessage, content: str) -> bool:
        content = " ".join(content.split())[: self.max_chars]
        if not content:
            return False
        await self.store.add(message.session_key, content, keep=self.max_items)
        return True

    async def prompt(self, message: InboundMessage) -> str:
        """给 chat 插件拼进系统提示的一段：已有的记忆 + 什么时候该记。"""
        items = await self.store.list(message.session_key)
        policy = self._policy.get(message.chat_scope, "explicit_only")
        when = _WHEN_TO_SAVE.get(policy, _WHEN_TO_SAVE["explicit_only"]).format(
            examples=self.preference_examples
        )
        lines = ["关于这位用户，你记得：", *(f"- {item}" for item in items)] if items else []
        lines.append(f"{when}每次只记一件事，用一句完整的话。不要记临时的问题和闲聊内容。")
        return "\n".join(lines)


class MemoryPlugin:
    id = "memory"

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config()
        if not cfg.get("enabled", False):
            logger.info("memory_plugin_disabled")
            return

        db = ctx.services.get("database")
        store: MemoryStore = PostgresMemoryStore(db) if db is not None else InMemoryMemoryStore()
        service = MemoryService(
            store,
            max_items=int(cfg.get("max_items") or 50),
            max_chars=int(cfg.get("max_chars") or 200),
            scope_cfg=cfg.get("scope") or {},
            preference_examples=str(cfg.get("preference_examples") or "").strip()
            or DEFAULT_PREFERENCE_EXAMPLES,
        )
        ctx.register_service("memory", service)

        async def tool_save(args: dict[str, Any], message: InboundMessage) -> Any:
            saved = await service.save(message, str(args.get("content") or ""))
            return {"saved": saved}

        async def erase_memories(message: InboundMessage) -> int:
            # 按人删：他在别的群里让机器人记住的也一起删（群聊记录、处理记录也是这个范围）
            return await store.clear(message.session_key, user_suffix=message.user_suffix)

        ctx.add_eraser("记忆", erase_memories)
        erasers = ctx.erasers  # 所有插件共用的那一份：后加载的插件登记的也在里面

        async def tool_forget_all(_args: dict[str, Any], message: InboundMessage) -> Any:
            deleted, failed = await erase_user_data(erasers, message)
            return {"deleted": deleted, "failed": failed}

        ctx.add_tool(
            "memory_save",
            "记住关于当前用户的一件事，以后的对话里都能看到。",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容，一句完整的话"}
                },
                "required": ["content"],
            },
            tool_save,
        )
        ctx.add_tool(
            "memory_forget_all",
            "删除机器人存下的关于当前用户的全部数据（记忆、群聊记录等）。"
            "仅在用户明确要求忘掉或删除他的信息时调用；返回每一类删了几条，failed 里是没删成的。",
            {"type": "object", "properties": {}},
            tool_forget_all,
        )

        commands = cfg.get("commands") or {}
        list_name = str(commands.get("list") or "记忆")
        forget_name = str(commands.get("forget") or "清除记忆")

        async def handle_list(message: InboundMessage, _args: str) -> OutboundMessage | None:
            items = await store.list(message.session_key)
            if not items:
                text = "我还没有记住关于你的任何事。"
            else:
                listing = "\n".join(f"{n}. {item}" for n, item in enumerate(items, start=1))
                text = f"我记得这些（发送 /{forget_name} 可全部删除）：\n{listing}"
            return OutboundMessage(content=text, reply_to=message.message_id)

        async def handle_forget(message: InboundMessage, _args: str) -> OutboundMessage | None:
            deleted, failed = await erase_user_data(erasers, message)
            parts = [f"{label} {count} 条" for label, count in deleted.items() if count]
            text = f"已删除：{'、'.join(parts)}。" if parts else "没有需要删除的数据。"
            if failed:
                text += f"\n{'、'.join(failed)}没有删成，请稍后再发一次 /{forget_name}。"
            return OutboundMessage(content=text, reply_to=message.message_id)

        ctx.add_command(list_name, handle_list, description="查看机器人记住了关于你的什么")
        ctx.add_command(forget_name, handle_forget, description="删除机器人存下的关于你的全部数据")
        logger.info("memory_plugin_enabled", backend=type(store).__name__)


def create_plugin() -> MemoryPlugin:
    return MemoryPlugin()
