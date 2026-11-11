"""群聊记录插件：把机器人在群里收到的消息、它自己的回复记进数据库，留着以后做分析、出日报。

只旁听、只写库：不回复，不改消息，机器人回答问题也不读这些记录。
没 @ 机器人的发言只有群主让机器人「获取群内全部消息」的群才收得到（见 gateway/qq.py）。
消息内容只进数据库，不进日志。

- 机器人的回复只记最终的那一条，「正在查资料…」这类进度不记。
- 用户要求删除他的数据时（``/清除记忆``，见 memory 插件）连他的群聊记录一起删：
  他的发言、机器人对这些发言的回复；别人的发言留着，但里面提到他的事件原文清空。
- 过了 ``keep_days`` 天的记录自动删掉。没有定时任务：每次记消息时看一眼，
  离上次清理超过 ``_PRUNE_EVERY_SECONDS`` 就清一次（所有群都没人说话的那段时间不会清，
  等下一条消息进来时补上）。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.chatlog.store import ChatEntry, ChatLogStore, PostgresChatLogStore

logger = get_logger(__name__)

_PRUNE_EVERY_SECONDS = 3600.0


class ChatLog:
    """记、删、清理，都经过这里。"""

    def __init__(self, store: ChatLogStore, *, scopes: set[str], keep_days: float = 0):
        self.store = store
        self.scopes = scopes
        #: 记录留多少天；0 = 一直留着
        self.keep_days = keep_days
        self._pruned_at: float | None = None

    async def record(self, message: InboundMessage) -> None:
        if message.chat_scope.value in self.scopes:
            await self.store.add(ChatEntry.of(message))
            await self.prune_if_due()

    async def record_reply(self, inbound: InboundMessage, outbound: OutboundMessage) -> None:
        if inbound.chat_scope.value in self.scopes:
            await self.store.add(ChatEntry.reply(inbound, outbound))

    async def forget(self, message: InboundMessage) -> int:
        """删掉发这条消息的人的群聊记录（所有群里的），返回删了他几条发言。"""
        return await self.store.forget(message.user_id)

    async def prune_if_due(self) -> int:
        """离上次清理够久了就删掉过期的记录；返回删了几条。"""
        now = time.monotonic()
        due = self._pruned_at is None or now - self._pruned_at >= _PRUNE_EVERY_SECONDS
        if self.keep_days <= 0 or not due:
            return 0
        self._pruned_at = now
        deleted = await self.store.prune(datetime.now(UTC) - timedelta(days=self.keep_days))
        if deleted:
            logger.info("chatlog_pruned", deleted=deleted, keep_days=self.keep_days)
        return deleted


class ChatLogPlugin:
    id = "chatlog"

    def __init__(self, store: ChatLogStore | None = None):
        #: 记到哪里；不给就用数据库（测试时给一个内存里的）
        self._store = store

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config()
        if not cfg.get("enabled", False):
            logger.info("chatlog_plugin_disabled")
            return
        store = self._store
        if store is None:
            db = ctx.services.get("database")
            if db is None:
                logger.info("chatlog_plugin_disabled", reason="没有数据库")
                return
            store = PostgresChatLogStore(db)
        log = ChatLog(
            store,
            scopes={str(scope) for scope in cfg.get("scopes") or ["group"]},
            keep_days=float(cfg.get("keep_days") or 0),
        )
        ctx.add_observer(log.record)
        ctx.add_reply_observer(log.record_reply)
        ctx.add_eraser("群聊记录", log.forget)
        ctx.register_service("chatlog", log)
        logger.info("chatlog_plugin_enabled", scopes=sorted(log.scopes), keep_days=log.keep_days)


def create_plugin() -> ChatLogPlugin:
    return ChatLogPlugin()
