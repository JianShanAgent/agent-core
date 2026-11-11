"""群聊记录的存储。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from jianshanagent.core.db import Database
from jianshanagent.core.messages import InboundMessage, OutboundMessage

_SECRET_PREFIXES = ("auth_token=",)  # 平台事件里夹带的一次性令牌，不入库
#: 机器人自己的回复，发言人一栏写这个
BOT_USER_ID = "bot"


def _scrub(value: Any) -> Any:
    """事件原文入库前去掉里面的令牌。

    令牌在 ``message_scene.ext`` 这个列表里，形如 ``auth_token=…``；被引用的消息嵌在里面，也带着。
    """
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [
            _scrub(item)
            for item in value
            if not (isinstance(item, str) and item.startswith(_SECRET_PREFIXES))
        ]
    return value


def _sent_at(timestamp: str) -> datetime:
    """平台给的发言时间（RFC 3339）；没给或者认不出来就用现在。"""
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return datetime.now(UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ChatEntry:
    """一条群聊记录，对应 ``chat_messages`` 表的一行。"""

    message_id: str
    scope: str
    chat_id: str
    user_id: str
    user_name: str | None
    content: str
    addressed: bool
    sent_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)
    #: 这一条是机器人发的回复
    from_bot: bool = False
    #: 机器人的回复：回的是哪条消息
    reply_to: str | None = None

    @classmethod
    def of(cls, message: InboundMessage) -> ChatEntry:
        return cls(
            message_id=message.message_id,
            scope=message.chat_scope.value,
            chat_id=message.chat_id,
            user_id=message.user_id,
            user_name=message.user_name,
            content=message.content,
            addressed=message.addressed,
            sent_at=_sent_at(message.timestamp),
            raw=_scrub(message.raw),
        )

    @classmethod
    def reply(cls, inbound: InboundMessage, outbound: OutboundMessage) -> ChatEntry:
        """机器人对 ``inbound`` 的最终回复。一条消息只有一条最终回复，编号就从被回复的那条来。"""
        return cls(
            message_id=f"reply:{inbound.message_id}",
            scope=inbound.chat_scope.value,
            chat_id=inbound.chat_id,
            user_id=BOT_USER_ID,
            user_name=None,
            content=outbound.content,
            addressed=False,
            sent_at=datetime.now(UTC),
            raw={"markdown": outbound.markdown, "buttons": [b.command for b in outbound.buttons]},
            from_bot=True,
            reply_to=inbound.message_id,
        )


class ChatLogStore(Protocol):
    async def add(self, entry: ChatEntry) -> bool:
        """记一条；同一条消息（按平台的消息 id）已经记过就不再记，返回 False。"""

    async def forget(self, user_id: str) -> int:
        """删掉一个人的记录，返回删了他几条发言。

        一并删掉机器人对这些发言的回复；别人的发言留着，但里面提到他的事件原文
        （被 @ 的人、被引用的消息都在原文里）清空。
        """

    async def prune(self, before: datetime) -> int:
        """删掉这个时间之前的记录，返回删了几条。"""


class InMemoryChatLogStore:
    """进程内实现：测试用。"""

    def __init__(self) -> None:
        self.entries: dict[str, ChatEntry] = {}

    async def add(self, entry: ChatEntry) -> bool:
        if entry.message_id in self.entries:
            return False
        self.entries[entry.message_id] = entry
        return True

    async def forget(self, user_id: str) -> int:
        if not user_id:
            return 0
        mine = {
            key
            for key, entry in self.entries.items()
            if entry.user_id == user_id and not entry.from_bot
        }
        for key, entry in list(self.entries.items()):
            if key in mine or (entry.from_bot and entry.reply_to in mine):
                del self.entries[key]
            elif user_id in json.dumps(entry.raw, ensure_ascii=False):
                self.entries[key] = replace(entry, raw={})
        return len(mine)

    async def prune(self, before: datetime) -> int:
        old = [key for key, entry in self.entries.items() if entry.sent_at < before]
        for key in old:
            del self.entries[key]
        return len(old)


class PostgresChatLogStore:
    """落在 ``chat_messages`` 表（见 migrations 的 0005、0006）。"""

    def __init__(self, db: Database):
        self._db = db

    async def add(self, entry: ChatEntry) -> bool:
        status: str = await self._db.pool.execute(
            "INSERT INTO chat_messages (message_id, scope, chat_id, user_id, user_name, content, "
            "addressed, sent_at, raw, from_bot, reply_to) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11) "
            "ON CONFLICT (message_id) DO NOTHING",
            entry.message_id,
            entry.scope,
            entry.chat_id,
            entry.user_id,
            entry.user_name,
            entry.content,
            entry.addressed,
            entry.sent_at,
            json.dumps(entry.raw, ensure_ascii=False),
            entry.from_bot,
            entry.reply_to,
        )
        return status.endswith(" 1")

    async def forget(self, user_id: str) -> int:
        if not user_id:  # 空的标识会匹配到所有人的事件原文
            return 0
        async with self._db.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "DELETE FROM chat_messages WHERE user_id = $1 AND NOT from_bot "
                "RETURNING message_id",
                user_id,
            )
            mine = [row["message_id"] for row in rows]
            if mine:
                await conn.execute(
                    "DELETE FROM chat_messages WHERE from_bot AND reply_to = ANY($1::text[])", mine
                )
            await conn.execute(
                "UPDATE chat_messages SET raw = '{}'::jsonb "
                "WHERE raw <> '{}'::jsonb AND position($1 in raw::text) > 0",
                user_id,
            )
        return len(mine)

    async def prune(self, before: datetime) -> int:
        status: str = await self._db.pool.execute(
            "DELETE FROM chat_messages WHERE sent_at < $1", before
        )
        return int(status.rsplit(" ", 1)[-1])
