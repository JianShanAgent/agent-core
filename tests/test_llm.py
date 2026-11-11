import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.llm import (
    LLMClient,
    LLMContentBlocked,
    LLMError,
    LLMProfile,
    LLMSettings,
    function_tool,
    tool_result_message,
    user_message,
)


def _settings(**overrides: Any) -> LLMSettings:
    base: dict[str, Any] = {
        "base_url": "https://llm.example/v1",
        "api_key": "tp-test",
        "profiles": {
            "chat": LLMProfile("chat", "mimo-v2.6-flash", thinking=False, temperature=0.8),
            "reason": LLMProfile("reason", "mimo-v2.6-pro", thinking=True, temperature=0.8),
            "extract": LLMProfile("extract", "mimo-v2.6-flash", thinking=False, temperature=0.2),
        },
        "max_retries": 2,
    }
    return LLMSettings(**{**base, **overrides})


def _reply(message: dict[str, Any], *, finish: str = "stop", usage: dict | None = None) -> dict:
    return {
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "model": "mimo-v2.6-flash",
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    settings: LLMSettings | None = None,
) -> LLMClient:
    return LLMClient(
        settings or _settings(),
        transport=httpx.MockTransport(handler),
        retry_base_seconds=0,
    )


def _recorder(
    responses: list[httpx.Response],
) -> tuple[list[dict[str, Any]], list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    bodies: list[dict[str, Any]] = []
    requests: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        bodies.append(json.loads(request.content))
        return queue.pop(0)

    return bodies, requests, handler


async def test_request_uses_api_key_header_and_explicit_thinking_off() -> None:
    bodies, requests, handler = _recorder(
        [
            httpx.Response(
                200, json=_reply({"role": "assistant", "content": "你好", "tool_calls": None})
            )
        ]
    )
    client = _client(handler)
    result = await client.chat([user_message("你好")])
    await client.aclose()

    assert requests[0].url == "https://llm.example/v1/chat/completions"
    assert requests[0].headers["api-key"] == "tp-test"
    body = bodies[0]
    assert body["model"] == "mimo-v2.6-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.8
    assert body["max_completion_tokens"] == 2048
    assert "tool_choice" not in body and "tools" not in body
    assert result.content == "你好"
    assert result.message == {"role": "assistant", "content": "你好"}


async def test_thinking_profile_omits_temperature() -> None:
    bodies, _, handler = _recorder(
        [httpx.Response(200, json=_reply({"role": "assistant", "content": "ok"}))]
    )
    client = _client(handler)
    await client.chat([user_message("x")], profile="reason")
    await client.aclose()

    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["model"] == "mimo-v2.6-pro"
    assert "temperature" not in bodies[0]


async def test_tool_call_turn_keeps_reasoning_content_for_next_request() -> None:
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "wiki_get", "arguments": '{"name": "山风"}'},
        }
    ]
    bodies, _, handler = _recorder(
        [
            httpx.Response(
                200,
                json=_reply(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls,
                        "reasoning_content": "先查山风",
                    },
                    finish="tool_calls",
                    usage={
                        "prompt_tokens": 50,
                        "completion_tokens": 20,
                        "completion_tokens_details": {"reasoning_tokens": 12},
                        "prompt_tokens_details": {"cached_tokens": 30},
                    },
                ),
            ),
            httpx.Response(200, json=_reply({"role": "assistant", "content": "山风是 SSR"})),
        ]
    )
    client = _client(handler)
    tools = [function_tool("wiki_get", "查式神", {"type": "object", "properties": {}})]
    history = [user_message("山风是什么稀有度")]

    first = await client.chat(history, profile="reason", tools=tools)
    assert first.finish_reason == "tool_calls"
    assert first.tool_calls[0].name == "wiki_get"
    assert first.tool_calls[0].arguments == {"name": "山风"}
    assert first.usage.reasoning_tokens == 12 and first.usage.cached_tokens == 30

    history += [first.message, tool_result_message("call_1", "稀有度 SSR")]
    second = await client.chat(history, profile="reason", tools=tools)
    await client.aclose()

    sent_assistant = bodies[1]["messages"][1]
    assert sent_assistant["reasoning_content"] == "先查山风"
    assert sent_assistant["tool_calls"] == tool_calls
    assert bodies[1]["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "稀有度 SSR",
    }
    assert second.content == "山风是 SSR"


async def test_malformed_tool_arguments_are_flagged_not_raised() -> None:
    _, _, handler = _recorder(
        [
            httpx.Response(
                200,
                json=_reply(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c",
                                "type": "function",
                                "function": {"name": "f", "arguments": "{oops"},
                            }
                        ],
                    },
                    finish="tool_calls",
                ),
            )
        ]
    )
    client = _client(handler)
    result = await client.chat([user_message("x")])
    await client.aclose()

    assert result.tool_calls[0].valid is False
    assert result.tool_calls[0].arguments == {}


async def test_multimodal_user_message_parts() -> None:
    message = user_message("这是谁", images=["https://img.example/a.png"])
    assert message["content"] == [
        {"type": "image_url", "image_url": {"url": "https://img.example/a.png"}},
        {"type": "text", "text": "这是谁"},
    ]
    assert user_message("纯文本") == {"role": "user", "content": "纯文本"}


async def test_retries_on_429_then_succeeds() -> None:
    bodies, _, handler = _recorder(
        [
            httpx.Response(429, text="rate limited"),
            httpx.Response(503, text="busy"),
            httpx.Response(200, json=_reply({"role": "assistant", "content": "ok"})),
        ]
    )
    client = _client(handler)
    result = await client.chat([user_message("x")])
    await client.aclose()

    assert result.content == "ok"
    assert len(bodies) == 3


async def test_gives_up_after_max_retries() -> None:
    bodies, _, handler = _recorder([httpx.Response(429, text="quota")] * 3)
    client = _client(handler)
    with pytest.raises(LLMError) as info:
        await client.chat([user_message("x")])
    await client.aclose()

    assert info.value.status == 429
    assert len(bodies) == 3


async def test_bad_request_is_not_retried() -> None:
    bodies, _, handler = _recorder([httpx.Response(400, text="reasoning_content missing")])
    client = _client(handler)
    with pytest.raises(LLMError) as info:
        await client.chat([user_message("x")])
    await client.aclose()

    assert info.value.status == 400 and info.value.retryable is False
    assert len(bodies) == 1


async def test_read_timeout_is_not_retried_but_connection_errors_are() -> None:
    calls = 0

    def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("still generating", request=request)

    client = _client(slow)
    with pytest.raises(LLMError, match="没有生成完"):  # 等满了还没生成完：重来只会让用户再等一遍
        await client.chat([user_message("x")])
    await client.aclose()
    assert calls == 1

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, json=_reply({"role": "assistant", "content": "ok"}))

    client = _client(flaky)
    assert (await client.chat([user_message("x")])).content == "ok"
    await client.aclose()
    assert calls == 4


async def test_a_200_that_is_not_a_model_reply_counts_as_a_failed_call() -> None:
    # 状态码 200 但内容不是模型的回复（中间的网关塞了错误页、形状不对）：
    # 上层只认 LLMError，抛别的异常用户看到的就是「处理消息时出错了」
    bodies: list[httpx.Response] = []
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return bodies.pop(0)

    client = LLMClient(
        _settings(max_retries=1), transport=httpx.MockTransport(handler), retry_base_seconds=0
    )
    # 一时的：再试一次就好了
    bodies[:] = [
        httpx.Response(200, text="<html>502 Bad Gateway</html>"),
        httpx.Response(200, json=_reply({"role": "assistant", "content": "好了"})),
    ]
    assert (await client.chat([user_message("嗨")], profile="chat")).content == "好了"
    assert len(calls) == 2

    # 一直这样：试够次数后是 LLMError
    for broken in (
        httpx.Response(200, text="<html>502 Bad Gateway</html>"),
        httpx.Response(200, json=["不是对象"]),
    ):
        calls.clear()
        bodies[:] = [broken, broken]
        with pytest.raises(LLMError, match="不是 JSON 对象"):
            await client.chat([user_message("嗨")], profile="chat")
        assert len(calls) == 2

    # 是个对象，但里面的形状不对
    for shape in ({"choices": "x"}, {"choices": ["x"]}, {"choices": [{"message": "x"}]}):
        bodies[:] = [httpx.Response(200, json=shape)]
        with pytest.raises(LLMError):
            await client.chat([user_message("嗨")], profile="chat")
    await client.aclose()

    # 连不上不用等满「等模型生成完」的时限（连接失败是会重试的那一类）
    timeout = LLMClient(_settings(timeout_seconds=150))._client.timeout
    assert (timeout.connect, timeout.read) == (10.0, 150.0)


async def test_content_moderation_block() -> None:
    client = _client(lambda _r: httpx.Response(421, text="blocked"))
    with pytest.raises(LLMContentBlocked):
        await client.chat([user_message("x")])
    await client.aclose()


class _Slots(BaseModel):
    name: str
    rarity: str | None = None


async def test_chat_json_validates_and_retries_with_error_feedback() -> None:
    bodies, _, handler = _recorder(
        [
            httpx.Response(200, json=_reply({"role": "assistant", "content": '{"rarity": "SSR"}'})),
            httpx.Response(
                200,
                json=_reply({"role": "assistant", "content": '{"name": "山风", "rarity": "SSR"}'}),
            ),
        ]
    )
    client = _client(handler)
    slots = await client.chat_json([user_message("只返回 JSON：name, rarity")], _Slots)
    await client.aclose()

    assert slots == _Slots(name="山风", rarity="SSR")
    assert bodies[0]["response_format"] == {"type": "json_object"}
    retry_messages = bodies[1]["messages"]
    assert retry_messages[1] == {"role": "assistant", "content": '{"rarity": "SSR"}'}
    assert "不符合要求" in retry_messages[2]["content"]


async def test_chat_json_raises_when_still_invalid() -> None:
    _, _, handler = _recorder(
        [httpx.Response(200, json=_reply({"role": "assistant", "content": "不是 JSON"}))] * 2
    )
    client = _client(handler)
    with pytest.raises(LLMError, match="仍不合法"):
        await client.chat_json([user_message("x")], _Slots)
    await client.aclose()


def _config(llm: dict[str, Any], secrets: dict[str, Any]) -> AppConfig:
    return AppConfig(root=Path("."), files={"core.llm": llm}, secrets=secrets)


def test_settings_disabled_returns_none() -> None:
    assert LLMSettings.from_config(_config({"enabled": False}, {})) is None


def test_settings_rejects_placeholder_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIANSHANAGENT_LLM_API_KEY", raising=False)
    llm = {
        "enabled": True,
        "base_url": "https://llm.example/v1",
        "profiles": {"chat": {"model": "m"}},
    }
    with pytest.raises(LLMError, match="没有可用的密钥"):
        LLMSettings.from_config(_config(llm, {"llm": {"api_key": "YOUR_MIMO_API_KEY"}}))


def test_repo_llm_config_parses_once_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIANSHANAGENT_LLM_API_KEY", "tp-test")
    repo = load_config(FULL_CONFIG_DIR)
    raw = {**repo.get("core.llm"), "enabled": True}
    settings = LLMSettings.from_config(_config(raw, {}))

    assert settings is not None
    assert settings.base_url.endswith("/v1")
    assert settings.profiles["chat"].thinking is True  # 关着时模型经常不查工具就编
    assert settings.profiles["reason"].thinking is True
    assert settings.default_profile in settings.profiles
    assert settings.history_max_turns == 5
    assert settings.history_idle_seconds == 7200
