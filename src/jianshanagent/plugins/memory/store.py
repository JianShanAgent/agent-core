"""记忆的存储。

作用域（scope）就是消息的 ``session_key``：单聊按用户，群聊按「群 + 群成员」。
QQ 在群里和单聊里给同一个人的标识不同，所以两边的记忆天然是分开的。
"""

from __future__ import annotations

from typing import Protocol

from jianshanagent.core.db import Database


class MemoryStore(Protocol):
    async def add(self, scope: str, content: str, *, keep: int) -> None:
        """记一条；重复内容不再记；超过 ``keep`` 条时丢掉最旧的。"""

    async def list(self, scope: str) -> list[str]:
        """从旧到新。"""

    async def clear(self, scope: str, *, user_suffix: str = "") -> int:
        """删除该作用域的全部记忆，返回删除条数。

        给了 ``user_suffix``（``InboundMessage.user_suffix``）时，以它结尾的作用域也一起删——
        同一个人在别的群里的记忆。
        """


class InMemoryMemoryStore:
    """进程内实现：没有数据库时的本机调试用，重启即清空。"""

    def __init__(self) -> None:
        self._data: dict[str, list[str]] = {}

    async def add(self, scope: str, content: str, *, keep: int) -> None:
        items = self._data.setdefault(scope, [])
        if content not in items:
            items.append(content)
            del items[:-keep]

    async def list(self, scope: str) -> list[str]:
        return list(self._data.get(scope, []))

    async def clear(self, scope: str, *, user_suffix: str = "") -> int:
        mine = [k for k in self._data if k == scope or (user_suffix and k.endswith(user_suffix))]
        return sum(len(self._data.pop(key)) for key in mine)


class PostgresMemoryStore:
    """落在 ``memories`` 表（见 migrations/0002_memories.sql）。"""

    def __init__(self, db: Database):
        self._db = db

    async def add(self, scope: str, content: str, *, keep: int) -> None:
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO memories (scope, content) VALUES ($1, $2) "
                "ON CONFLICT (scope, content) DO NOTHING",
                scope,
                content,
            )
            await conn.execute(
                "DELETE FROM memories WHERE scope = $1 AND id NOT IN "
                "(SELECT id FROM memories WHERE scope = $1 ORDER BY id DESC LIMIT $2)",
                scope,
                keep,
            )

    async def list(self, scope: str) -> list[str]:
        rows = await self._db.pool.fetch(
            "SELECT content FROM memories WHERE scope = $1 ORDER BY id", scope
        )
        return [row["content"] for row in rows]

    async def clear(self, scope: str, *, user_suffix: str = "") -> int:
        status = await self._db.pool.execute(
            "DELETE FROM memories WHERE scope = $1 "
            "OR ($2 <> '' AND right(scope, char_length($2)) = $2)",
            scope,
            user_suffix,
        )
        return int(status.rsplit(" ", 1)[-1])
