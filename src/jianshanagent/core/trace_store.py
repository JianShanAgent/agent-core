"""处理记录落到数据库（``traces`` / ``llm_calls``，见 migrations/0007_traces.sql）。

只管写和清理；查询在管理后台那边（admin/queries.py）。
"""

from __future__ import annotations

import json
from typing import Any

from jianshanagent.core.db import Database
from jianshanagent.core.trace import LLMCall, Trace


def _json(value: Any) -> str:
    # 过程里填的东西不一定都是 JSON 认识的类型（日期、集合…），认不出来的转成文字
    return json.dumps(value, ensure_ascii=False, default=str)


class PostgresTraceStore:
    def __init__(self, db: Database):
        self._db = db

    async def save_trace(self, trace: Trace) -> None:
        row = trace.to_json()
        await self._db.pool.execute(
            "INSERT INTO traces (id, started_at, seconds, channel, scope, chat_id, user_id, "
            "user_name, session_key, message_id, question, images, route, command, status, "
            "outcome, reply, error, llm_calls, prompt_tokens, completion_tokens, "
            "reasoning_tokens, steps, summary) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, "
            "$18, $19, $20, $21, $22, $23::jsonb, $24::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            trace.id,
            trace.started_at,
            trace.seconds,
            trace.channel,
            trace.scope,
            trace.chat_id,
            trace.user_id,
            trace.user_name,
            trace.session_key,
            trace.message_id,
            trace.question,
            trace.images,
            trace.route,
            trace.command,
            trace.status,
            trace.outcome,
            trace.reply,
            trace.error,
            row["llm_calls"],
            row["prompt_tokens"],
            row["completion_tokens"],
            row["reasoning_tokens"],
            _json(row["steps"]),
            _json(row["summary"]),
        )

    async def save_call(self, call: LLMCall) -> None:
        await self._db.pool.execute(
            "INSERT INTO llm_calls (trace_id, step_id, started_at, seconds, purpose, profile, "
            "model, thinking, ok, finish_reason, error, prompt_tokens, completion_tokens, "
            "reasoning_tokens, cached_tokens, tool_calls, request, response) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, "
            "$17::jsonb, $18::jsonb)",
            call.trace_id,
            call.step_id,
            call.started_at,
            call.seconds,
            call.purpose,
            call.profile,
            call.model,
            call.thinking,
            call.ok,
            call.finish_reason,
            call.error,
            call.prompt_tokens,
            call.completion_tokens,
            call.reasoning_tokens,
            call.cached_tokens,
            call.tool_calls,
            _json(call.request) if call.request is not None else None,
            _json(call.response) if call.response is not None else None,
        )

    async def erase_user(self, session_key: str, user_id: str) -> int:
        """删掉一个用户的全部处理记录（连同它们的模型调用），返回删了几条。

        用户要求删除他的数据时用（见 runtime.py 里登记的那一项）。删的是他在所有群里的，
        不只是他提出要求的这个群（平台在各个群里给同一个人的标识是同一个）；
        ``session_key`` 是他提出要求时所在的会话，一并删。
        """
        pool = self._db.pool
        mine = "session_key = $1 OR (user_id = $2 AND $2 <> '')"
        await pool.execute(
            f"DELETE FROM llm_calls WHERE trace_id IN (SELECT id FROM traces WHERE {mine})",
            session_key,
            user_id,
        )
        status = await pool.execute(f"DELETE FROM traces WHERE {mine}", session_key, user_id)
        return int(status.rsplit(" ", 1)[-1])

    async def erase_trace(self, trace_id: str) -> None:
        """删掉一条记录和它名下的模型调用（用户要求删除数据的那条消息自己的，见 Tracer.finish）。"""
        pool = self._db.pool
        await pool.execute("DELETE FROM llm_calls WHERE trace_id = $1", trace_id)
        await pool.execute("DELETE FROM traces WHERE id = $1", trace_id)

    async def prune(self, *, prompt_days: int, trace_days: int) -> tuple[int, int]:
        """清理过期的内容，返回（清空了提示词的调用数，删掉的处理记录数）。

        ``prompt_days``：提示词和回复留多少天，过期的清空（用量数字留着）；
        ``trace_days``：处理记录留多少天，过期的连同它的模型调用一起删。都是 0 = 一直留着。
        """
        cleared = removed = 0
        pool = self._db.pool
        if prompt_days > 0:
            status = await pool.execute(
                "UPDATE llm_calls SET request = NULL, response = NULL "
                "WHERE (request IS NOT NULL OR response IS NOT NULL) "
                "AND started_at < now() - make_interval(days => $1)",
                prompt_days,
            )
            cleared = int(status.rsplit(" ", 1)[-1])
        if trace_days > 0:
            await pool.execute(
                "DELETE FROM llm_calls WHERE started_at < now() - make_interval(days => $1)",
                trace_days,
            )
            status = await pool.execute(
                "DELETE FROM traces WHERE started_at < now() - make_interval(days => $1)",
                trace_days,
            )
            removed = int(status.rsplit(" ", 1)[-1])
        return cleared, removed
