"""供应商 HTTP 尝试级计量。只有显式打开计量作用域的请求才会记账。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx


@dataclass(frozen=True, slots=True)
class UsageCall:
    call_id: str
    request_id: str
    app_id: str
    started_at: datetime
    kind: str
    model: str


@dataclass(frozen=True, slots=True)
class UsageResult:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    embedding_tokens: int | None = None
    search_tokens: int | None = None
    searches: int | None = None


class UsageStore(Protocol):
    async def begin_call(self, call: UsageCall) -> None:
        """在发送 HTTP 请求前持久化尝试，所有用量字段都未知。"""
        ...

    async def finish_call(self, call: UsageCall, result: UsageResult) -> None:
        """更新这条尝试的用量，仅记录供应商已经报告的数值。"""
        ...


class UsagePersistenceError(RuntimeError):
    """计量存储失败；不属于供应商请求错误，也不应触发重试。"""


_scope: ContextVar[tuple[UsageStore, str, str] | None] = ContextVar("usage_scope", default=None)
_pending_finishes: set[asyncio.Task[None]] = set()


@contextmanager
def usage_scope(store: UsageStore, app_id: str, request_id: str) -> Iterator[None]:
    token = _scope.set((store, app_id, request_id))
    try:
        yield
    finally:
        _scope.reset(token)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def llm_usage(data: dict[str, Any]) -> UsageResult:
    usage = _object(data.get("usage"))
    return UsageResult(
        prompt_tokens=_count(usage.get("prompt_tokens")),
        completion_tokens=_count(usage.get("completion_tokens")),
        cached_tokens=_count(_object(usage.get("prompt_tokens_details")).get("cached_tokens")),
        reasoning_tokens=_count(
            _object(usage.get("completion_tokens_details")).get("reasoning_tokens")
        ),
    )


def embedding_usage(data: dict[str, Any]) -> UsageResult:
    return UsageResult(embedding_tokens=_count(_object(data.get("usage")).get("prompt_tokens")))


def mimo_search_usage(data: dict[str, Any]) -> UsageResult:
    usage = _object(data.get("usage"))
    return UsageResult(
        search_tokens=_count(usage.get("total_tokens")),
        searches=_count(_object(usage.get("web_search_usage")).get("tool_usage")),
    )


def gitee_search_usage(data: dict[str, Any]) -> UsageResult:
    # 这是单次直接搜索。成功时计一次，判定与搜索客户端一致；它不报告 Token。
    accepted = data.get("code") in (None, 200) and not data.get("error")
    return UsageResult(searches=1 if accepted else None)


async def _finish_call(store: UsageStore, call: UsageCall, result: UsageResult) -> None:
    try:
        await store.finish_call(call, result)
    except Exception:
        raise UsagePersistenceError("无法保存供应商用量") from None


def _release_finish(task: asyncio.Task[None]) -> None:
    _pending_finishes.discard(task)
    if not task.cancelled():
        task.exception()  # 如果请求已取消，仍然取走异常，避免事件循环记录栈和上下文。


async def drain_usage(timeout: float = 2.0) -> None:
    """入口关停后，给已收到的用量一个写完的机会，超时则保留已登记的未知记录。"""
    if _pending_finishes:
        _, pending = await asyncio.wait(tuple(_pending_finishes), timeout=timeout)
        for task in pending:
            task.cancel()


async def metered_post(
    client: httpx.AsyncClient,
    path: str,
    *,
    body: dict[str, Any],
    kind: str,
    model: str,
    read_usage: Callable[[dict[str, Any]], UsageResult],
    usage_only_on_success: bool = False,
) -> httpx.Response:
    """每次调用对应一次 HTTP 尝试；在客户端处理状态码和业务数据前保存用量。"""
    scope = _scope.get()
    if scope is None:
        return await client.post(path, json=body)
    store, app_id, request_id = scope
    request = client.build_request("POST", path, json=body)
    call = UsageCall(
        call_id=str(uuid4()),
        request_id=request_id,
        app_id=app_id,
        started_at=datetime.now(UTC),
        kind=kind,
        model=model,
    )
    try:
        await store.begin_call(call)
    except Exception:
        raise UsagePersistenceError("无法登记供应商调用") from None

    # 连接、读响应失败或被取消时，初始行已足以表示本次用量未知。
    response = await client.send(request)
    try:
        data = response.json()
    except (ValueError, RecursionError):
        data = None
    result = (
        read_usage(data)
        if isinstance(data, dict) and (not usage_only_on_success or response.status_code == 200)
        else UsageResult()
    )
    # 已收到供应商报告时，请求取消不应中断持久化，但仍向调用方传播取消。
    finish = asyncio.create_task(_finish_call(store, call, result))
    _pending_finishes.add(finish)
    finish.add_done_callback(_release_finish)
    await asyncio.shield(finish)
    return response
