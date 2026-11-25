"""供应商计量只测 loopback URL 和 HTTP 替身，不访问真实接口。"""

import asyncio
import json
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from jianshanagent.core.embedding import EmbeddingClient, EmbeddingError, EmbeddingProvider
from jianshanagent.core.llm import LLMClient, LLMError, LLMProfile, LLMSettings, user_message
from jianshanagent.core.trace import LLMCall
from jianshanagent.core.usage import UsageCall, UsagePersistenceError, UsageResult, usage_scope
from jianshanagent.core.websearch import WebSearchClient, WebSearchError, WebSearchSettings

URL = "http://127.0.0.1:9999/v1"
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


class Store:
    def __init__(self) -> None:
        self.calls: list[UsageCall] = []
        self.results: dict[str, UsageResult] = {}
        self.finished: list[str] = []

    async def begin_call(self, call: UsageCall) -> None:
        assert call.call_id not in self.results
        assert call.started_at.tzinfo is UTC
        self.calls.append(call)
        self.results[call.call_id] = UsageResult()

    async def finish_call(self, call: UsageCall, result: UsageResult) -> None:
        assert call in self.calls and call.call_id not in self.finished
        self.finished.append(call.call_id)
        self.results[call.call_id] = result


def _chat(handler: Handler, *, retries: int = 0) -> LLMClient:
    return LLMClient(
        LLMSettings(
            base_url=URL,
            api_key="key-not-for-ledger",
            profiles={"chat": LLMProfile("chat", "chat-model", thinking=False)},
            max_retries=retries,
        ),
        transport=httpx.MockTransport(handler),
        retry_base_seconds=0,
    )


def _answer(**fields: Any) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], **fields})


async def test_concurrent_apps_use_request_local_scope_and_unscoped_calls_do_not_count() -> None:
    store = Store()
    two_in_flight = asyncio.Event()
    seen: list[str] = []
    tokens = {"one": 4, "two": 9, "no scope": 10}

    async def respond(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        seen.append(prompt)
        if prompt != "no scope":
            assert any(call.request_id == f"request-{prompt}" for call in store.calls)
            if len(seen) == 2:
                two_in_flight.set()
            await asyncio.wait_for(two_in_flight.wait(), timeout=5)
        return _answer(usage={"prompt_tokens": tokens[prompt], "completion_tokens": 0})

    chat = _chat(respond)

    async def run(app: str) -> None:
        with usage_scope(store, f"app-{app}", f"request-{app}"):
            assert (await chat.chat([user_message(app)])).content == "ok"

    try:
        await asyncio.gather(run("one"), run("two"))
        with usage_scope(store, "app-failed", "request-failed"), pytest.raises(ValueError):
            raise ValueError("the scope must reset even if its body raises")
        assert (await chat.chat([user_message("no scope")])).usage.prompt_tokens == 10
    finally:
        await chat.aclose()

    assert len(seen) == 3
    recorded = {
        (call.app_id, call.request_id, call.kind, call.model): store.results[call.call_id]
        for call in store.calls
    }
    assert recorded == {
        ("app-one", "request-one", "llm", "chat-model"): UsageResult(
            prompt_tokens=4, completion_tokens=0
        ),
        ("app-two", "request-two", "llm", "chat-model"): UsageResult(
            prompt_tokens=9, completion_tokens=0
        ),
    }


async def test_every_retry_has_its_own_row_and_reports_precede_semantic_parsing() -> None:
    store = Store()
    sent = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        assert len(store.calls) == sent  # begin already durable before each outbound attempt
        assert store.results[store.calls[-1].call_id] == UsageResult()
        if sent == 1:
            raise httpx.ConnectError("no response", request=request)
        if sent == 2:
            return httpx.Response(
                429, json={"usage": {"prompt_tokens": 2, "completion_tokens": True}}
            )
        if sent == 3:
            return httpx.Response(503, text="busy")
        assert len(store.finished) == 2
        return httpx.Response(
            200,
            json={
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
                "choices": [],
            },
        )

    chat = _chat(respond, retries=3)
    traced: list[LLMCall] = []
    chat.on_call = traced.append
    try:
        with usage_scope(store, "app", "req"), pytest.raises(LLMError, match="没有 choices"):
            await chat.chat([user_message("not-for-ledger")])
    finally:
        await chat.aclose()

    ids = [call.call_id for call in store.calls]
    assert sent == len(ids) == len(set(ids)) == 4
    assert store.finished == ids[1:]
    assert [store.results[key] for key in ids] == [
        UsageResult(),
        UsageResult(prompt_tokens=2),
        UsageResult(),
        UsageResult(prompt_tokens=8, completion_tokens=3, cached_tokens=0, reasoning_tokens=1),
    ]
    assert len(traced) == 1 and not traced[0].ok
    assert "not-for-ledger" not in repr((store.calls, store.results))
    assert "key-not-for-ledger" not in repr((store.calls, store.results))


async def test_generated_json_retry_does_not_replace_previous_report() -> None:
    class Answer(BaseModel):
        value: int

    responses = iter(
        [
            _answer(usage={"completion_tokens": 5}),
            _answer(
                choices=[{"message": {"content": '{"value": 2}'}}],
                usage={"completion_tokens": 7},
            ),
        ]
    )
    store = Store()
    chat = _chat(lambda _: next(responses))
    try:
        with usage_scope(store, "app", "req"):
            answer = await chat.chat_json([user_message("JSON")], Answer, profile="chat")
    finally:
        await chat.aclose()
    assert answer.value == 2
    assert list(store.results.values()) == [
        UsageResult(completion_tokens=5),
        UsageResult(completion_tokens=7),
    ]


async def test_embedding_retry_and_invalid_vectors_still_record_reported_input_tokens() -> None:
    store = Store()
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"data": [], "usage": {"prompt_tokens": 11}}),
        ]
    )
    client = EmbeddingClient(
        EmbeddingProvider("test", URL, "embed-model", max_retries=1),
        transport=httpx.MockTransport(lambda _: next(responses)),
        retry_base_seconds=0,
    )
    try:
        with usage_scope(store, "app", "req"):
            assert not (await client.embed([])).vectors
            with pytest.raises(EmbeddingError, match="0 个向量"):
                await client.embed(["private"])
    finally:
        await client.aclose()

    assert [(call.kind, call.model) for call in store.calls] == [("embedding", "embed-model")] * 2
    assert list(store.results.values()) == [UsageResult(), UsageResult(embedding_tokens=11)]


@pytest.mark.parametrize("provider,model", [("mimo", "search-model"), ("gitee_ai", "gitee_ai")])
async def test_search_has_its_own_counters_and_does_not_infer_tokens(
    provider: str, model: str
) -> None:
    store = Store()
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(
                200,
                json={
                    "error": "response cannot be used",
                    "usage": {"total_tokens": 7, "web_search_usage": {"tool_usage": 2}},
                }
                if provider == "mimo"
                else {"data": {"webPages": {"value": []}}},
            ),
        ]
    )
    client = WebSearchClient(
        WebSearchSettings(provider, URL, "key", model="search-model", max_retries=1),
        transport=httpx.MockTransport(lambda _: next(responses)),
        retry_base_seconds=0,
    )
    try:
        with usage_scope(store, "app", "req"):
            assert await client.search(" ") == []
            if provider == "mimo":
                with pytest.raises(WebSearchError):
                    await client.search("private")
            else:
                assert await client.search("private") == []
    finally:
        await client.aclose()

    assert [(call.kind, call.model) for call in store.calls] == [("search", model)] * 2
    assert list(store.results.values()) == [
        UsageResult(),
        UsageResult(search_tokens=7, searches=2) if provider == "mimo" else UsageResult(searches=1),
    ]


@pytest.mark.parametrize("phase", ["begin", "finish"])
async def test_persistence_errors_are_sanitized_and_are_not_http_retried(phase: str) -> None:
    class FailingStore(Store):
        async def begin_call(self, call: UsageCall) -> None:
            if phase == "begin" and self.calls:
                raise httpx.ConnectError("sensitive database configuration")
            await super().begin_call(call)

        async def finish_call(self, call: UsageCall, result: UsageResult) -> None:
            if phase == "finish" and self.finished:
                raise httpx.ConnectError("sensitive database configuration")
            await super().finish_call(call, result)

    store = FailingStore()
    sent = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        if sent == 1:
            return httpx.Response(503, json={"usage": {"prompt_tokens": 5}})
        return _answer(usage={"prompt_tokens": 9})

    chat = _chat(respond, retries=3)
    traced: list[LLMCall] = []
    chat.on_call = traced.append
    try:
        with usage_scope(store, "app", "req"), pytest.raises(UsagePersistenceError) as caught:
            await chat.chat([user_message("test")])
    finally:
        await chat.aclose()
    assert sent == len(store.calls) == (1 if phase == "begin" else 2)
    assert len(traced) == 1 and not traced[0].ok
    formatted = "".join(traceback.format_exception(caught.value))
    assert "sensitive database configuration" not in formatted + traced[0].error
    assert list(store.results.values()) == [UsageResult(prompt_tokens=5)] + (
        [UsageResult()] if phase == "finish" else []
    )


@pytest.mark.parametrize("response_already_arrived", [False, True])
async def test_cancellation_keeps_attempt_unknown_or_finishes_received_usage(
    response_already_arrived: bool,
) -> None:
    waiting = asyncio.Event()
    may_finish = asyncio.Event()
    did_finish = asyncio.Event()

    class PausingStore(Store):
        async def finish_call(self, call: UsageCall, result: UsageResult) -> None:
            waiting.set()
            await may_finish.wait()
            await super().finish_call(call, result)
            did_finish.set()

    async def respond(request: httpx.Request) -> httpx.Response:
        if not response_already_arrived:
            waiting.set()
            await asyncio.Future[None]()
        return _answer(usage={"prompt_tokens": 21})

    store = PausingStore()
    chat = _chat(respond)
    traced: list[LLMCall] = []
    chat.on_call = traced.append
    try:
        with usage_scope(store, "app", "req"):
            task = asyncio.create_task(chat.chat([user_message("test")]))
            await asyncio.wait_for(waiting.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(store.calls) == 1 and list(store.results.values()) == [UsageResult()]
        assert len(traced) == 1 and not traced[0].ok
        if response_already_arrived:
            may_finish.set()
            await asyncio.wait_for(did_finish.wait(), timeout=5)
            assert list(store.results.values()) == [UsageResult(prompt_tokens=21)]
        else:
            assert not store.finished
    finally:
        may_finish.set()
        await chat.aclose()
