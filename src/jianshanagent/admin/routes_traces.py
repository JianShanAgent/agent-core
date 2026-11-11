"""处理记录（每条消息怎么处理的）和模型调用记录。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aiohttp import web

from jianshanagent.admin.core import (
    Admin,
    fail,
    flag_param,
    int_param,
    reply,
    text_param,
    time_param,
)
from jianshanagent.admin.routes_system import trace_summary
from jianshanagent.core.trace import LLMCall, Trace


def _call_row(call: LLMCall, number: int) -> dict[str, Any]:
    """内存里的一次模型调用 → 和数据库查出来的一样的字段（还没落库，编号用负数）。"""
    row = asdict(call)
    row["id"] = -number
    row["has_detail"] = call.request is not None or call.response is not None
    row.pop("request")
    row.pop("response")
    return row


def _live(trace: Trace, *, finished: bool) -> dict[str, Any]:
    found = trace.to_json(finished=finished)
    found["calls"] = [_call_row(call, number) for number, call in enumerate(trace.calls, start=1)]
    return found


def routes(admin: Admin) -> list[web.RouteDef]:
    async def list_traces(request: web.Request) -> web.Response:
        limit = int_param(request, "limit", 50, 1, 200)
        tracer = admin.runtime.tracer
        active = [trace_summary(trace, finished=False) for trace in tracer.active.values()]
        if not admin.has_database:
            recent = [trace_summary(trace, finished=True) for trace in reversed(tracer.recent)]
            return reply(
                {"items": recent[:limit], "active": active, "more": False, "stored": False}
            )
        min_seconds = text_param(request, "min_seconds")
        try:
            slow = float(min_seconds) if min_seconds else None
        except ValueError:
            fail(400, "参数 min_seconds 应该是数字")
        items = await admin.queries.traces(
            limit=limit + 1,
            before=time_param(request, "before"),
            channel=text_param(request, "channel"),
            route=text_param(request, "route"),
            status=text_param(request, "status"),
            outcome=text_param(request, "outcome"),
            session_key=text_param(request, "session"),
            chat_id=text_param(request, "chat"),
            search=text_param(request, "q"),
            min_seconds=slow,
            problems_only=flag_param(request, "problems"),
        )
        more = len(items) > limit
        return reply(
            {
                "items": await admin.labelled(items[:limit]),
                "active": await admin.labelled(active),
                "more": more,
                "stored": True,
            }
        )

    async def get_trace(request: web.Request) -> web.Response:
        trace_id = request.match_info["id"]
        tracer = admin.runtime.tracer
        live = tracer.active.get(trace_id)
        if live is not None:
            return reply((await admin.labelled([_live(live, finished=False)]))[0])
        if admin.has_database:
            found = await admin.queries.trace(trace_id)
            if found is not None:
                return reply((await admin.labelled([found]))[0])
        # 刚处理完、还没落库的（或者没有数据库）：内存里最近的几十条里找
        for trace in tracer.recent:
            if trace.id == trace_id:
                return reply((await admin.labelled([_live(trace, finished=True)]))[0])
        fail(404, "没有这条处理记录（可能已经过了保留期）")

    async def list_calls(request: web.Request) -> web.Response:
        limit = int_param(request, "limit", 100, 1, 500)
        before = text_param(request, "before")
        items = await admin.queries.calls(
            limit=limit + 1,
            before=int(before) if before and before.lstrip("-").isdigit() else None,
            profile=text_param(request, "profile"),
            purpose=request.query.get("purpose") if "purpose" in request.query else None,
            failed_only=flag_param(request, "failed"),
        )
        return reply({"items": items[:limit], "more": len(items) > limit})

    async def get_call(request: web.Request) -> web.Response:
        raw = request.match_info["id"]
        trace_id = text_param(request, "trace")
        if raw.startswith("-") and trace_id:
            # 还没落库的调用：编号是它在那条处理记录里的序号（负数）
            tracer = admin.runtime.tracer
            candidates = [tracer.active.get(trace_id), *tracer.recent]
            for trace in candidates:
                if trace is not None and trace.id == trace_id and 0 < -int(raw) <= len(trace.calls):
                    call = trace.calls[-int(raw) - 1]
                    return reply(
                        {
                            **_call_row(call, -int(raw)),
                            "request": call.request,
                            "response": call.response,
                        }
                    )
            fail(404, "没有这次模型调用")
        if not raw.isdigit():
            fail(400, "编号不对")
        found = await admin.queries.call(int(raw))
        if found is None:
            fail(404, "没有这次模型调用")
        found["has_detail"] = found.get("request") is not None or found.get("response") is not None
        return reply(found)

    return [
        web.get("/api/traces", list_traces),
        web.get("/api/traces/{id}", get_trace),
        web.get("/api/llm-calls", list_calls),
        web.get("/api/llm-calls/{id}", get_call),
    ]
