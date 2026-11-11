"""多轮对话历史的存储。

为什么要落库而不只放内存：MiMo 在思考模式下要求，历史里带 ``tool_calls`` 的
assistant 消息必须原样带回 ``reasoning_content``（缺失返回 400，且官方说明
会导致幻觉增多）。所以这里按接口的原始消息格式整条保存，机器人重启后仍能
把完整历史带回去。

保存与取回时的规则：

- 保存：媒体内容（图片/音频/视频）替换成文字占位。QQ 的附件链接有时效，
  过期后再带回会让整个请求 400。
- 取回：以「轮」为单位（一轮 = 一条 user 消息到下一条 user 消息之前，含中间的
  工具调用和工具结果），不会留下孤立的 tool 消息或半截工具调用。
  先按轮数取最近 ``max_turns`` 轮，再在这几轮里按时间截断：从最新一轮往回看，
  遇到超过 ``idle_seconds`` 的空档就停，空档之前的不带。空档指「现在距最新一轮」
  或「相邻两轮之间」的间隔，所以隔了很久再开口等于新对话，之后也不会把旧话题带回来。

数据库里的历史不会因为过期被删除，只是不再带给模型；``clear`` 才真正删除。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from jianshanagent.core.db import Database

Message = dict[str, Any]
Stamped = tuple[float, Message]  # （保存时刻的 Unix 秒，消息）

DEFAULT_MAX_TURNS = 5
DEFAULT_IDLE_SECONDS = 7200.0

_MEDIA_PLACEHOLDERS = {
    "image_url": "[图片]",
    "input_audio": "[语音]",
    "video_url": "[视频]",
}


def strip_media(message: Message) -> Message:
    """把多模态内容压成纯文本，返回可长期保存的消息副本。"""
    content = message.get("content")
    if not isinstance(content, list):
        return dict(message)
    pieces: list[str] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            pieces.append(str(part.get("text") or ""))
        elif kind in _MEDIA_PLACEHOLDERS:
            pieces.append(_MEDIA_PLACEHOLDERS[kind])
    return {**message, "content": "".join(pieces)}


def select_recent_turns(
    entries: Sequence[Stamped],
    *,
    now: float,
    max_turns: int = DEFAULT_MAX_TURNS,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
) -> list[Message]:
    """从按时间正序的消息里挑出要带给模型的历史。规则见模块说明。

    ``idle_seconds`` 小于等于 0 表示不按时间过期；``max_turns`` 至少按 1 轮算。
    """
    turns: list[list[Stamped]] = []
    for entry in entries:
        if entry[1].get("role") == "user":
            turns.append([entry])
        elif turns:  # 第一条 user 之前的零散消息属于被截掉一半的轮次，丢弃
            turns[-1].append(entry)

    kept: list[list[Stamped]] = []
    newer_started_at = now
    for turn in reversed(turns[-max(max_turns, 1) :]):
        if idle_seconds > 0 and newer_started_at - turn[-1][0] > idle_seconds:
            break
        kept.append(turn)
        newer_started_at = turn[0][0]
    return [message for turn in reversed(kept) for _, message in turn]


class ConversationStore(Protocol):
    async def append(self, session_key: str, messages: Sequence[Message]) -> None: ...

    async def history(
        self,
        session_key: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
    ) -> list[Message]: ...

    async def clear(self, session_key: str, *, user_suffix: str = "") -> int:
        """删掉一个会话的对话历史，返回删了几条消息。

        给了 ``user_suffix``（``InboundMessage.user_suffix``）时，以它结尾的会话也一起删——
        同一个人在别的群里的（用户要求删除他的数据时用）。
        """
        ...


class MemoryConversationStore:
    """进程内实现：用于测试，以及没有数据库时的本机调试。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._data: dict[str, list[Stamped]] = {}
        self._clock = clock

    async def append(self, session_key: str, messages: Sequence[Message]) -> None:
        now = self._clock()
        self._data.setdefault(session_key, []).extend((now, strip_media(m)) for m in messages)

    async def history(
        self,
        session_key: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
    ) -> list[Message]:
        return select_recent_turns(
            self._data.get(session_key, []),
            now=self._clock(),
            max_turns=max_turns,
            idle_seconds=idle_seconds,
        )

    async def clear(self, session_key: str, *, user_suffix: str = "") -> int:
        mine = [
            key
            for key in self._data
            if key == session_key or (user_suffix and key.endswith(user_suffix))
        ]
        return sum(len(self._data.pop(key)) for key in mine)


class PostgresConversationStore:
    """落在 ``llm_messages`` 表（见 migrations/0001_llm_messages.sql）。"""

    def __init__(self, db: Database):
        self._db = db

    async def append(self, session_key: str, messages: Sequence[Message]) -> None:
        if not messages:
            return
        rows = [
            (session_key, str(m.get("role") or ""), json.dumps(strip_media(m), ensure_ascii=False))
            for m in messages
        ]
        # 同一批消息在一个事务里写入，保证一轮工具调用不会只存下一半
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                "INSERT INTO llm_messages (session_key, role, message) VALUES ($1, $2, $3::jsonb)",
                rows,
            )

    async def history(
        self,
        session_key: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
    ) -> list[Message]:
        # 子查询找到倒数第 max_turns 条 user 消息，从它开始取，正好是最近 max_turns 轮；
        # 不足 max_turns 轮时子查询没有结果，取全部。时间都用数据库的时钟，不受容器时钟影响。
        records = await self._db.pool.fetch(
            "SELECT message::text AS message, "
            "       extract(epoch FROM created_at)::float8 AS saved_at, "
            "       extract(epoch FROM now())::float8 AS now "
            "FROM llm_messages "
            "WHERE session_key = $1 AND id >= COALESCE(("
            "    SELECT id FROM llm_messages WHERE session_key = $1 AND role = 'user' "
            "    ORDER BY id DESC OFFSET $2 LIMIT 1), 0) "
            "ORDER BY id",
            session_key,
            max(max_turns, 1) - 1,
        )
        if not records:
            return []
        return select_recent_turns(
            [(record["saved_at"], json.loads(record["message"])) for record in records],
            now=records[0]["now"],
            max_turns=max_turns,
            idle_seconds=idle_seconds,
        )

    async def clear(self, session_key: str, *, user_suffix: str = "") -> int:
        status = await self._db.pool.execute(
            "DELETE FROM llm_messages WHERE session_key = $1 "
            "OR ($2 <> '' AND right(session_key, char_length($2)) = $2)",
            session_key,
            user_suffix,
        )
        return int(status.rsplit(" ", 1)[-1])
