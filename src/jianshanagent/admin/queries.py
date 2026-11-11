"""管理后台读数据库的地方：统计、处理记录、会话与记忆、群聊记录、知识库浏览、备注名。

只有这里直接写 SQL。大多是只读的；删除只有三处（清空某个会话的对话历史、删记忆、改备注名），
都是管理后台里明确点了才会做的。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from jianshanagent.core.db import Database

#: 用时分布的分档（秒）
DURATION_EDGES = (5, 10, 20, 30, 60, 120)
#: 会话清单、群聊清单默认只看最近多少天有动静的（页面上可以换，0 = 不限时间）
SESSION_DAYS = 30
CHAT_DAYS = 7
_TRACE_COLUMNS = (
    "id, started_at, seconds, channel, scope, chat_id, user_id, user_name, session_key, "
    "message_id, images, route, command, status, outcome, error, llm_calls, prompt_tokens, "
    "completion_tokens, reasoning_tokens, summary"
)
_CALL_COLUMNS = (
    "id, trace_id, step_id, started_at, seconds, purpose, profile, model, thinking, ok, "
    "finish_reason, error, prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens, "
    "tool_calls, (request IS NOT NULL OR response IS NOT NULL) AS has_detail"
)


def _plain(row: Any, *, json_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """数据库的一行 → 能直接转成 JSON 的字典（时间转成文字，JSON 列解开）。"""
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif key in json_fields and isinstance(value, str):
            out[key] = json.loads(value)
        else:
            out[key] = value
    return out


def _like(text: str) -> str:
    """用户输入的搜索词 → ILIKE 的写法（里面的 % 和 _ 当普通字符）。"""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def split_session(session_key: str) -> dict[str, str]:
    """会话键 → 会话类型、会话标识、成员标识（单聊没有成员）。"""
    scope, _, rest = session_key.partition(":")
    if scope not in ("c2c", "dm") and ":user:" in rest:  # 多人的会话（群、频道）带着发言人
        chat_id, user_id = rest.split(":user:", 1)
        return {"scope": scope, "chat_id": chat_id, "user_id": user_id}
    return {"scope": scope, "chat_id": rest, "user_id": ""}


class Where:
    """拼 WHERE：条件和参数一起攒，参数的编号自己数。"""

    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause: str, *values: Any) -> None:
        """``clause`` 里用 ``{}`` 占参数的位置，按顺序换成 $1、$2……"""
        marks = []
        for value in values:
            self.params.append(value)
            marks.append(f"${len(self.params)}")
        self.clauses.append(clause.format(*marks))

    def param(self, value: Any) -> str:
        self.params.append(value)
        return f"${len(self.params)}"

    @property
    def sql(self) -> str:
        return ("WHERE " + " AND ".join(self.clauses)) if self.clauses else ""


class AdminQueries:
    def __init__(self, db: Database, timezone: str = "Asia/Shanghai"):
        self._db = db
        #: 按小时、按天归档时用哪个时区的钟
        self.timezone = timezone

    # ---- 统计 ----

    async def overview(
        self, since: datetime, *, channel: str | None, daily: bool
    ) -> dict[str, Any]:
        """从 ``since`` 到现在的概况。``channel``：只算哪个来源（qq / web），None = 都算。"""
        pool = self._db.pool
        unit = "day" if daily else "hour"
        trace_filter = "started_at >= $1 AND ($2::text IS NULL OR channel = $2)"
        call_filter = (
            "c.started_at >= $1 AND ($2::text IS NULL OR c.trace_id IN "
            "(SELECT id FROM traces WHERE channel = $2 AND started_at >= $1 - interval '1 hour'))"
        )
        edges = DURATION_EDGES
        buckets = ", ".join(
            [f"count(*) FILTER (WHERE seconds < {edges[0]}) AS d0"]
            + [
                f"count(*) FILTER (WHERE seconds >= {low} AND seconds < {high}) AS d{index}"
                for index, (low, high) in enumerate(zip(edges, edges[1:], strict=False), start=1)
            ]
            + [f"count(*) FILTER (WHERE seconds >= {edges[-1]}) AS d{len(edges)}"]
        )
        totals = await pool.fetchrow(
            "SELECT count(*) AS messages, "
            "count(*) FILTER (WHERE status = 'replied') AS replied, "
            "count(*) FILTER (WHERE status = 'error') AS errors, "
            "count(*) FILTER (WHERE route = 'research') AS research, "
            "count(*) FILTER (WHERE outcome = 'answered') AS answered, "
            "count(*) FILTER (WHERE outcome = 'not_found') AS not_found, "
            "count(*) FILTER (WHERE outcome = 'failed') AS failed, "
            "count(*) FILTER (WHERE route = 'command') AS commands, "
            "count(*) FILTER (WHERE route = 'chat') AS chitchat, "
            "count(*) FILTER (WHERE route = 'limited') AS limited, "
            "count(DISTINCT session_key) AS users, "
            "count(DISTINCT chat_id) FILTER (WHERE scope = 'group') AS groups, "
            "coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds), 0) AS median_seconds, "
            "coalesce(percentile_cont(0.9) WITHIN GROUP (ORDER BY seconds), 0) AS p90_seconds, "
            "coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) "
            "  FILTER (WHERE route = 'research'), 0) AS research_median_seconds, "
            "coalesce(percentile_cont(0.9) WITHIN GROUP (ORDER BY seconds) "
            "  FILTER (WHERE route = 'research'), 0) AS research_p90_seconds "
            f"FROM traces WHERE {trace_filter}",
            since,
            channel,
        )
        durations = await pool.fetchrow(
            f"SELECT {buckets} FROM traces WHERE {trace_filter} AND route = 'research'",
            since,
            channel,
        )
        llm = await pool.fetchrow(
            "SELECT count(*) AS llm_calls, count(*) FILTER (WHERE NOT c.ok) AS llm_failed, "
            "coalesce(sum(c.prompt_tokens), 0) AS prompt_tokens, "
            "coalesce(sum(c.completion_tokens), 0) AS completion_tokens, "
            "coalesce(sum(c.reasoning_tokens), 0) AS reasoning_tokens, "
            "coalesce(sum(c.cached_tokens), 0) AS cached_tokens "
            f"FROM llm_calls c WHERE {call_filter}",
            since,
            channel,
        )
        trace_series = await pool.fetch(
            "SELECT date_trunc($3, started_at AT TIME ZONE $4) AS bucket, count(*) AS messages, "
            "count(*) FILTER (WHERE route = 'research') AS research, "
            "count(*) FILTER (WHERE status = 'error' OR outcome = 'failed') AS errors, "
            "coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) "
            "  FILTER (WHERE route = 'research'), 0) AS median_seconds "
            f"FROM traces WHERE {trace_filter} GROUP BY 1",
            since,
            channel,
            unit,
            self.timezone,
        )
        call_series = await pool.fetch(
            "SELECT date_trunc($3, c.started_at AT TIME ZONE $4) AS bucket, "
            "count(*) AS llm_calls, "
            "coalesce(sum(c.prompt_tokens), 0) AS prompt_tokens, "
            "coalesce(sum(c.completion_tokens), 0) AS completion_tokens "
            f"FROM llm_calls c WHERE {call_filter} GROUP BY 1",
            since,
            channel,
            unit,
            self.timezone,
        )
        routes = await pool.fetch(
            f"SELECT route, count(*) AS count FROM traces WHERE {trace_filter} "
            "GROUP BY route ORDER BY count DESC",
            since,
            channel,
        )
        topics = await pool.fetch(
            "SELECT coalesce(nullif(summary->'research'->>'topic', ''), '没归类') AS topic, "
            f"count(*) AS count FROM traces WHERE {trace_filter} AND route = 'research' "
            "GROUP BY 1 ORDER BY count DESC",
            since,
            channel,
        )
        grouped = (
            "count(*) AS calls, count(*) FILTER (WHERE NOT c.ok) AS failed, "
            "coalesce(sum(c.prompt_tokens), 0) AS prompt_tokens, "
            "coalesce(sum(c.completion_tokens), 0) AS completion_tokens, "
            "coalesce(sum(c.reasoning_tokens), 0) AS reasoning_tokens, "
            "avg(c.seconds) AS avg_seconds, "
            "percentile_cont(0.9) WITHIN GROUP (ORDER BY c.seconds) AS p90_seconds"
        )
        profiles = await pool.fetch(
            f"SELECT c.profile, max(c.model) AS model, bool_or(c.thinking) AS thinking, {grouped} "
            f"FROM llm_calls c WHERE {call_filter} GROUP BY c.profile ORDER BY calls DESC",
            since,
            channel,
        )
        purposes = await pool.fetch(
            f"SELECT c.purpose, {grouped} FROM llm_calls c WHERE {call_filter} "
            "GROUP BY c.purpose ORDER BY calls DESC",
            since,
            channel,
        )
        return {
            "totals": {**_plain(totals), **_plain(llm)},
            "durations": [int(durations[f"d{index}"]) for index in range(len(edges) + 1)],
            "trace_series": {row["bucket"]: _plain(row) for row in trace_series},
            "call_series": {row["bucket"]: _plain(row) for row in call_series},
            "routes": [_plain(row) for row in routes],
            "topics": [_plain(row) for row in topics],
            "profiles": [_plain(row) for row in profiles],
            "purposes": [_plain(row) for row in purposes],
        }

    async def profile_usage(self, since: datetime) -> dict[str, dict[str, Any]]:
        """各档模型从 ``since`` 到现在的调用情况（模型管理页用）。"""
        rows = await self._db.pool.fetch(
            "SELECT profile, count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS failed, "
            "avg(seconds) AS avg_seconds, "
            "percentile_cont(0.9) WITHIN GROUP (ORDER BY seconds) AS p90_seconds, "
            "coalesce(sum(prompt_tokens + completion_tokens), 0) AS tokens, "
            "max(started_at) AS last_at "
            "FROM llm_calls WHERE started_at >= $1 GROUP BY profile",
            since,
        )
        return {row["profile"]: _plain(row) for row in rows}

    # ---- 处理记录 ----

    async def traces(
        self,
        *,
        limit: int,
        before: datetime | None = None,
        channel: str | None = None,
        route: str | None = None,
        status: str | None = None,
        outcome: str | None = None,
        session_key: str | None = None,
        chat_id: str | None = None,
        search: str | None = None,
        min_seconds: float | None = None,
        problems_only: bool = False,
    ) -> list[dict[str, Any]]:
        where = Where()
        if before is not None:
            where.add("started_at < {}", before)
        if channel:
            where.add("channel = {}", channel)
        if route:
            where.add("route = {}", route)
        if status:
            where.add("status = {}", status)
        if outcome:
            where.add("outcome = {}", outcome)
        if session_key:
            where.add("session_key = {}", session_key)
        if chat_id:
            where.add("chat_id = {}", chat_id)
        if search:
            where.add("(question ILIKE {} OR reply ILIKE {})", _like(search), _like(search))
        if min_seconds:
            where.add("seconds >= {}", min_seconds)
        if problems_only:
            # 值得看一眼的：出错的、没查到的、被复核打回过的
            where.clauses.append(
                "(status = 'error' OR outcome IN ('not_found', 'failed') "
                "OR coalesce((summary->'research'->>'rejections')::int, 0) > 0)"
            )
        rows = await self._db.pool.fetch(
            f"SELECT {_TRACE_COLUMNS}, left(question, 300) AS question, left(reply, 300) AS reply "
            f"FROM traces {where.sql} ORDER BY started_at DESC LIMIT {where.param(limit)}",
            *where.params,
        )
        return [_plain(row, json_fields=("summary",)) for row in rows]

    async def trace(self, trace_id: str) -> dict[str, Any] | None:
        pool = self._db.pool
        row = await pool.fetchrow("SELECT * FROM traces WHERE id = $1", trace_id)
        if row is None:
            return None
        calls = await pool.fetch(
            f"SELECT {_CALL_COLUMNS} FROM llm_calls WHERE trace_id = $1 ORDER BY id", trace_id
        )
        found = _plain(row, json_fields=("steps", "summary"))
        found["finished"] = True
        found["calls"] = [_plain(call) for call in calls]
        return found

    async def calls(
        self,
        *,
        limit: int,
        before: int | None = None,
        profile: str | None = None,
        purpose: str | None = None,
        failed_only: bool = False,
    ) -> list[dict[str, Any]]:
        where = Where()
        if before is not None:
            where.add("id < {}", before)
        if profile:
            where.add("profile = {}", profile)
        if purpose is not None and purpose != "":
            where.add("purpose = {}", purpose)
        if failed_only:
            where.clauses.append("(NOT ok OR finish_reason NOT IN ('stop', 'tool_calls', ''))")
        rows = await self._db.pool.fetch(
            f"SELECT {_CALL_COLUMNS} FROM llm_calls {where.sql} "
            f"ORDER BY id DESC LIMIT {where.param(limit)}",
            *where.params,
        )
        return [_plain(row) for row in rows]

    async def call(self, call_id: int) -> dict[str, Any] | None:
        row = await self._db.pool.fetchrow("SELECT * FROM llm_calls WHERE id = $1", call_id)
        return _plain(row, json_fields=("request", "response")) if row is not None else None

    # ---- 会话、记忆 ----

    async def sessions(
        self, *, limit: int, offset: int, search: str | None, days: int = SESSION_DAYS
    ) -> dict[str, Any]:
        """最近 ``days`` 天有过对话历史、记忆或处理记录的会话，最近活动的在前（0 = 不限时间）。

        页面开着就会隔一会儿来取一次，而这几张表只增不减，所以分两步、都不扫整张表：
        先只看最近 ``days`` 天的行（走时间索引）找出有哪些会话、各自最后活动的时间；
        排好序、切出这一页以后，再按会话键去数这几十个会话各有多少条、取最近的昵称
        （走会话键的索引）。条数是这个会话全部的，不只是这几天的。
        不限时间时第一步还是要把三张表过一遍——页面上选「全部」才会这样，而且不自动刷新。

        搜索：会话键、这段时间里处理记录上出现过的昵称、备注名。
        """
        pattern = _like(search) if search else None
        since = datetime.now(UTC) - timedelta(days=days) if days > 0 else None
        # 时间条件直接拼进语句，不写成「$2 IS NULL OR …」：那样写数据库没法走时间索引
        params: list[Any] = [pattern] if since is None else [pattern, since]

        def recent(column: str, lead: str = "WHERE") -> str:
            return f"{lead} {column} >= $2" if since is not None else ""

        body = (
            "WITH active AS ("
            "  SELECT session_key, max(created_at) AS last_at "
            f"  FROM llm_messages {recent('created_at')} GROUP BY 1 "
            "  UNION ALL "
            f"  SELECT scope, max(created_at) FROM memories {recent('created_at')} GROUP BY 1 "
            "  UNION ALL "
            "  SELECT session_key, max(started_at) "
            f"  FROM traces {recent('started_at')} GROUP BY 1), "
            "found AS (SELECT session_key, max(last_at) AS last_at FROM active GROUP BY 1), "
            # 也能按备注名搜：备注名是给会话或成员的标识起的，标识出现在会话键里
            "matched AS ("
            "  SELECT * FROM found f WHERE ($1::text IS NULL OR f.session_key ILIKE $1 "
            "    OR EXISTS (SELECT 1 FROM traces t WHERE t.session_key = f.session_key "
            f"               AND t.user_name ILIKE $1 {recent('t.started_at', 'AND')}) "
            "    OR EXISTS (SELECT 1 FROM admin_labels l "
            "               WHERE l.label ILIKE $1 AND position(l.id in f.session_key) > 0)))"
        )
        pool = self._db.pool
        rows = await pool.fetch(
            # 一共有多少个顺便数出来（count(*) OVER ()），不为它把上面那一段再跑一遍
            f"{body}, page AS ("
            "  SELECT *, count(*) OVER () AS total FROM matched "
            "  ORDER BY last_at DESC NULLS LAST, session_key "
            f"  LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}) "
            "SELECT p.session_key, h.messages, h.turns, m.memories, t.traces, "
            "       n.user_name, c.channel, p.last_at, p.total "
            "FROM page p "
            "CROSS JOIN LATERAL ("
            "  SELECT count(*) AS messages, count(*) FILTER (WHERE role = 'user') AS turns "
            "  FROM llm_messages WHERE session_key = p.session_key) h "
            "CROSS JOIN LATERAL ("
            "  SELECT count(*) AS memories FROM memories WHERE scope = p.session_key) m "
            "CROSS JOIN LATERAL ("
            "  SELECT count(*) AS traces FROM traces WHERE session_key = p.session_key) t "
            "LEFT JOIN LATERAL ("
            "  SELECT user_name FROM traces "
            "  WHERE session_key = p.session_key AND user_name IS NOT NULL "
            "  ORDER BY started_at DESC LIMIT 1) n ON true "
            "LEFT JOIN LATERAL ("
            "  SELECT channel FROM traces WHERE session_key = p.session_key "
            "  ORDER BY started_at DESC LIMIT 1) c ON true "
            "ORDER BY p.last_at DESC NULLS LAST, p.session_key",
            *params,
            limit,
            offset,
        )
        if rows:
            total = rows[0]["total"]
        elif offset:
            # 翻过头了（看的时候会话变少了）：这一页是空的，带不出总数，单独数一次
            total = await pool.fetchval(f"{body} SELECT count(*) FROM matched", *params)
        else:
            total = 0
        items = []
        for row in rows:
            item = {**_plain(row), **split_session(row["session_key"])}
            del item["total"]
            items.append(item)
        return {"total": int(total or 0), "items": items}

    async def session(self, session_key: str, *, messages: int = 300) -> dict[str, Any]:
        pool = self._db.pool
        history = await pool.fetch(
            "SELECT id, role, message::text AS message, created_at FROM llm_messages "
            "WHERE session_key = $1 ORDER BY id DESC LIMIT $2",
            session_key,
            messages,
        )
        memories = await pool.fetch(
            "SELECT id, content, created_at FROM memories WHERE scope = $1 ORDER BY id", session_key
        )
        total = await pool.fetchval(
            "SELECT count(*) FROM llm_messages WHERE session_key = $1", session_key
        )
        return {
            "session_key": session_key,
            **split_session(session_key),
            "total_messages": int(total or 0),
            "messages": [_plain(row, json_fields=("message",)) for row in reversed(history)],
            "memories": [_plain(row) for row in memories],
        }

    async def clear_history(self, session_key: str) -> int:
        status = await self._db.pool.execute(
            "DELETE FROM llm_messages WHERE session_key = $1", session_key
        )
        return int(status.rsplit(" ", 1)[-1])

    async def delete_memory(self, memory_id: int) -> int:
        status = await self._db.pool.execute("DELETE FROM memories WHERE id = $1", memory_id)
        return int(status.rsplit(" ", 1)[-1])

    async def clear_memories(self, session_key: str) -> int:
        status = await self._db.pool.execute("DELETE FROM memories WHERE scope = $1", session_key)
        return int(status.rsplit(" ", 1)[-1])

    # ---- 群聊记录 ----

    async def chats(self, *, days: int = CHAT_DAYS) -> list[dict[str, Any]]:
        """最近 ``days`` 天有消息的群，和这些天里各自的条数、发言人数（0 = 不限时间）。

        页面开着就会隔一会儿来取一次：限定了天数就只读这几天的行（走时间索引），不扫整张表。
        所以条数、人数、最早一条都是「这几天里」的，不是这个群全部的。
        """
        since = datetime.now(UTC) - timedelta(days=days) if days > 0 else None
        rows = await self._db.pool.fetch(
            # 分组和数人头要先排序。这几列是平台给的标识，按字节比（COLLATE "C"）就行，
            # 比按语言习惯比快好几倍（实测不限时间时 2 秒 → 0.5 秒）
            'SELECT chat_id COLLATE "C" AS chat_id, scope COLLATE "C" AS scope, '
            "count(*) AS messages, count(*) FILTER (WHERE addressed) AS addressed, "
            'count(DISTINCT user_id COLLATE "C") AS members, '
            "min(sent_at) AS first_at, max(sent_at) AS last_at, "
            "count(*) FILTER (WHERE sent_at >= now() - interval '24 hours') AS last_day "
            f"FROM chat_messages {'WHERE sent_at >= $1' if since is not None else ''} "
            "GROUP BY 1, 2 ORDER BY max(sent_at) DESC",
            *([since] if since is not None else []),
        )
        return [_plain(row) for row in rows]

    async def chat_messages(
        self,
        chat_id: str,
        *,
        limit: int,
        before: int | None = None,
        search: str | None = None,
        addressed_only: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """某个群的记录，新的在前。"""
        where = Where()
        where.add("chat_id = {}", chat_id)
        if before is not None:
            where.add("id < {}", before)
        if search:
            where.add("(content ILIKE {} OR user_name ILIKE {})", _like(search), _like(search))
        if addressed_only:
            where.clauses.append("addressed")
        if user_id:
            where.add("user_id = {}", user_id)
        rows = await self._db.pool.fetch(
            "SELECT id, message_id, user_id, user_name, content, addressed, sent_at, "
            "CASE WHEN jsonb_typeof(raw->'attachments') = 'array' "
            "     THEN jsonb_array_length(raw->'attachments') ELSE 0 END AS attachments "
            f"FROM chat_messages {where.sql} ORDER BY id DESC LIMIT {where.param(limit)}",
            *where.params,
        )
        return [_plain(row) for row in rows]

    # ---- 知识库 ----

    async def kb_summary(self) -> dict[str, Any]:
        pool = self._db.pool
        types = await pool.fetch(
            "SELECT type, count(*) AS chunks, count(DISTINCT doc_key) AS docs, "
            "count(embedding) AS with_vectors, coalesce(sum(length(text)), 0) AS chars "
            "FROM kb_chunks GROUP BY type ORDER BY chunks DESC"
        )
        kinds = await pool.fetch(
            "SELECT kind, count(*) AS images, count(embedding) AS with_vectors "
            "FROM kb_images GROUP BY kind ORDER BY images DESC"
        )
        return {"types": [_plain(row) for row in types], "image_kinds": [_plain(r) for r in kinds]}

    async def kb_sync(self) -> dict[str, Any] | None:
        """知识库自动更新（kbsync 服务）最近一轮写下的状态；它还没跑过就是 None。

        状态是 plugins/knowledge/sync.py 的 ``status_of`` 写进 ``kb_meta`` 表 ``sync`` 这一行的。
        """
        raw = await self._db.pool.fetchval("SELECT value FROM kb_meta WHERE key = 'sync'")
        status = json.loads(raw) if raw else None
        if not isinstance(status, dict):
            return None
        status.pop("loaded", None)  # 库里装的是哪一版文件：给 kbsync 自己对账用的，页面上用不着
        return status

    async def kb_chunks(
        self, *, limit: int, offset: int, kind: str | None, search: str | None
    ) -> dict[str, Any]:
        where = Where()
        if kind:
            where.add("type = {}", kind)
        if search:
            where.add("(title ILIKE {} OR text ILIKE {})", _like(search), _like(search))
        pool = self._db.pool
        total = await pool.fetchval(f"SELECT count(*) FROM kb_chunks {where.sql}", *where.params)
        rows = await pool.fetch(
            "SELECT chunk_id, doc_key, type, title, section_path, seq, date, date_inferred, url, "
            "source, length(text) AS chars, left(text, 240) AS preview, "
            "(embedding IS NOT NULL) AS has_vector "
            f"FROM kb_chunks {where.sql} ORDER BY title, doc_key, seq "
            f"LIMIT {where.param(limit)} OFFSET {where.param(offset)}",
            *where.params,
        )
        return {"total": int(total or 0), "items": [_plain(row) for row in rows]}

    async def kb_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        pool = self._db.pool
        row = await pool.fetchrow(
            "SELECT chunk_id, doc_key, type, title, section_path, seq, text, url, source, date, "
            "date_inferred, entities, meta, (embedding IS NOT NULL) AS has_vector, fingerprint, "
            "updated_at FROM kb_chunks WHERE chunk_id = $1",
            chunk_id,
        )
        if row is None:
            return None
        siblings = await pool.fetch(
            "SELECT chunk_id, seq, section_path, length(text) AS chars FROM kb_chunks "
            "WHERE doc_key = $1 ORDER BY seq, chunk_id",
            row["doc_key"],
        )
        found = _plain(row, json_fields=("entities", "meta"))
        found["siblings"] = [_plain(item) for item in siblings]
        return found

    async def kb_images(
        self, *, limit: int, offset: int, kind: str | None, search: str | None
    ) -> dict[str, Any]:
        where = Where()
        if kind:
            where.add("kind = {}", kind)
        if search:
            pattern = _like(search)
            where.add(
                "(label ILIKE {} OR page_title ILIKE {} OR image_id ILIKE {})",
                pattern,
                pattern,
                pattern,
            )
        pool = self._db.pool
        total = await pool.fetchval(f"SELECT count(*) FROM kb_images {where.sql}", *where.params)
        rows = await pool.fetch(
            "SELECT image_id, kind, label, subject, page_title, url, image_url, text, "
            "(embedding IS NOT NULL) AS has_vector "
            f"FROM kb_images {where.sql} ORDER BY page_title, image_id "
            f"LIMIT {where.param(limit)} OFFSET {where.param(offset)}",
            *where.params,
        )
        return {"total": int(total or 0), "items": [_plain(row) for row in rows]}

    # ---- 备注名 ----

    async def labels(self) -> dict[str, dict[str, str]]:
        """全部备注名：``{"chat": {标识: 名字}, "user": {标识: 名字}}``。"""
        rows = await self._db.pool.fetch("SELECT kind, id, label FROM admin_labels")
        out: dict[str, dict[str, str]] = {"chat": {}, "user": {}}
        for row in rows:
            out.setdefault(row["kind"], {})[row["id"]] = row["label"]
        return out

    async def set_label(self, kind: str, target: str, label: str) -> None:
        """起备注名；``label`` 是空的就是去掉。"""
        pool = self._db.pool
        if not label:
            await pool.execute("DELETE FROM admin_labels WHERE kind = $1 AND id = $2", kind, target)
            return
        await pool.execute(
            "INSERT INTO admin_labels (kind, id, label) VALUES ($1, $2, $3) "
            "ON CONFLICT (kind, id) DO UPDATE SET label = EXCLUDED.label, updated_at = now()",
            kind,
            target,
            label,
        )

    async def user_names(self, user_ids: list[str]) -> dict[str, str]:
        """群成员最近用的昵称（平台给了才有）。"""
        if not user_ids:
            return {}
        rows = await self._db.pool.fetch(
            "SELECT DISTINCT ON (user_id) user_id, user_name FROM chat_messages "
            "WHERE user_id = ANY($1::text[]) AND user_name IS NOT NULL "
            "ORDER BY user_id, sent_at DESC",
            user_ids,
        )
        return {row["user_id"]: row["user_name"] for row in rows}

    # ---- 数据库概况 ----

    async def backup_state(self) -> dict[str, Any]:
        """数据库备份的情况：最近一次的结果、最近一次成功是什么时候、从什么时候开始有这份记录。

        结果是备份容器每次备份完写进 ``backup_runs`` 的（deploy/backup/backup.sh）。
        ``since`` 是这张表建出来的时间：一次成功的都没有时，从它算起过了多久。
        """
        pool = self._db.pool
        last = await pool.fetchrow(
            "SELECT finished_at, ok, file, bytes, copied, detail FROM backup_runs "
            "ORDER BY finished_at DESC, id DESC LIMIT 1"
        )
        marks = await pool.fetchrow(
            "SELECT (SELECT max(finished_at) FROM backup_runs WHERE ok) AS last_ok_at, "
            "(SELECT applied_at FROM schema_migrations "
            " WHERE name = '0009_backup_runs.sql') AS since"
        )
        return {
            "last": dict(last) if last else None,
            "last_ok_at": marks["last_ok_at"],
            "since": marks["since"],
        }

    async def tables(self) -> list[dict[str, Any]]:
        rows = await self._db.pool.fetch(
            "SELECT relname AS name, n_live_tup AS rows, pg_total_relation_size(relid) AS bytes "
            "FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC"
        )
        return [_plain(row) for row in rows]


def fill_series(
    start: datetime,
    end: datetime,
    *,
    daily: bool,
    traces: dict[Any, dict[str, Any]],
    calls: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    """把按小时（或按天）的统计补成连续的：没有消息的时段也占一格，图上才不会断。

    ``start`` / ``end`` 是统计用的那个时区的钟点（不带时区）。
    """
    step = timedelta(days=1) if daily else timedelta(hours=1)
    moment = start.replace(minute=0, second=0, microsecond=0)
    if daily:
        moment = moment.replace(hour=0)
    out: list[dict[str, Any]] = []
    while moment <= end:
        trace = traces.get(moment, {})
        call = calls.get(moment, {})
        out.append(
            {
                "t": moment.isoformat(),
                "messages": int(trace.get("messages") or 0),
                "research": int(trace.get("research") or 0),
                "errors": int(trace.get("errors") or 0),
                "median_seconds": float(trace.get("median_seconds") or 0),
                "llm_calls": int(call.get("llm_calls") or 0),
                "prompt_tokens": int(call.get("prompt_tokens") or 0),
                "completion_tokens": int(call.get("completion_tokens") or 0),
            }
        )
        moment += step
    return out
