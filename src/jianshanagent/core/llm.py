"""LLM 调用层：小米 MiMo 的 OpenAI 兼容 Chat Completions 接口。

只依赖 httpx，不引入 openai SDK。几条来自官方文档的约束在这里统一处理：

- ``thinking`` 只有 enabled / disabled，没有强度档位；开启时 temperature 不生效。
- 思考模式下，历史里带 ``tool_calls`` 的 assistant 消息必须原样带回
  ``reasoning_content``，否则接口返回 400。因此 :attr:`LLMResult.message`
  保留模型返回的原始字段，调用方把它整条追加进历史即可。
- 结构化输出只有 ``json_object``（保证是合法 JSON，不保证字段），需要自行校验。
- ``tool_choice`` 只支持 auto，所以不发送该字段。
- 图片（2026-11-11 实测，``mimo-v2.6-pro`` 和 ``mimo-v2.6-flash`` 都能看图）：user 消息里的
  ``image_url`` 收公网地址，也收 ``data:image/jpeg;base64,…``；开思考、带 ``tools`` 时照常工作，
  带图的消息后面接着工具结果再请求也可以。长边 1024 的 JPEG 一张算 1024 个输入 token。
  给的不是图片（格式认不出、地址取不到）时返回 400 ``Param Incorrect``，
  说明里写着只支持 bmp / gif / png / jpeg / webp。
  带图的请求大多 2～25 秒返回（pro 开思考，34 次里 30 次）；另有 3 次等了 80～160 秒，
  还有等了几十秒后返回 ``finish_reason`` 为 ``abort``、一个 token 都没有的空回复
  （pro、flash 各 1 次）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger
from jianshanagent.core.trace import LLMCall, brief, compact_messages

logger = get_logger(__name__)

Message = dict[str, Any]
ModelT = TypeVar("ModelT", bound=BaseModel)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
#: 连模型接口最多等这么久（等它生成完的时限是配置里的 timeout_seconds，长得多）
_CONNECT_TIMEOUT_SECONDS = 10.0


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMContentBlocked(LLMError):
    """输入或输出被平台内容审核拦截（HTTP 421）。"""


@dataclass(frozen=True, slots=True)
class LLMProfile:
    name: str
    model: str
    thinking: bool = False
    max_completion_tokens: int = 2048
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class LLMSettings:
    base_url: str
    api_key: str
    profiles: dict[str, LLMProfile]
    default_profile: str = "chat"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    history_max_turns: int = 5
    history_idle_seconds: float = 7200.0

    @classmethod
    def from_config(cls, config: AppConfig) -> LLMSettings | None:
        """读取 core.llm.yaml 与密钥；``enabled: false`` 时返回 None。"""
        raw = config.get("core.llm")
        if not raw.get("enabled", False):
            return None

        api_key = str(
            os.environ.get("JIANSHANAGENT_LLM_API_KEY")
            or (config.secrets.get("llm") or {}).get("api_key")
            or ""
        ).strip()
        if not api_key or api_key.upper().startswith("YOUR_"):
            raise LLMError(
                "core.llm.yaml 已启用，但没有可用的密钥："
                "请填写 secrets.yaml 的 llm.api_key（或环境变量 JIANSHANAGENT_LLM_API_KEY）"
            )
        base_url = str(raw.get("base_url") or "").rstrip("/")
        if not base_url:
            raise LLMError("core.llm.yaml 缺少 base_url")

        profiles: dict[str, LLMProfile] = {}
        for name, item in (raw.get("profiles") or {}).items():
            if not item.get("model"):
                raise LLMError(f"core.llm.yaml 的 profiles.{name} 缺少 model")
            temperature = item.get("temperature")
            profiles[name] = LLMProfile(
                name=name,
                model=str(item["model"]),
                thinking=bool(item.get("thinking", False)),
                max_completion_tokens=int(item.get("max_completion_tokens") or 2048),
                temperature=float(temperature) if temperature is not None else None,
            )
        default_profile = str(raw.get("default_profile") or "chat")
        if default_profile not in profiles:
            raise LLMError(
                f"core.llm.yaml 的 default_profile「{default_profile}」未在 profiles 中定义"
            )

        history = raw.get("history") or {}
        idle = history.get("idle_expire_seconds")
        return cls(
            base_url=base_url,
            api_key=api_key,
            profiles=profiles,
            default_profile=default_profile,
            timeout_seconds=float(raw.get("timeout_seconds") or 60),
            max_retries=int(raw.get("max_retries") if raw.get("max_retries") is not None else 2),
            history_max_turns=int(history.get("max_turns") or 5),
            history_idle_seconds=float(idle) if idle is not None else 7200.0,  # 0 = 不过期
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    #: 模型给出的参数不是合法 JSON 对象时为 False（官方文档提示这种情况会发生）
    valid: bool = True


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


@dataclass(slots=True)
class LLMResult:
    #: 可直接追加进对话历史的 assistant 消息（保留 reasoning_content 与 tool_calls）
    message: Message
    content: str
    reasoning: str
    tool_calls: list[ToolCall]
    #: stop / length / tool_calls / content_filter / repetition_truncation；
    #: 文档里没有、实测出现过的还有 abort（正文和工具调用都是空的，见模块说明的图片一条）
    finish_reason: str
    usage: LLMUsage
    model: str


def user_message(
    text: str,
    *,
    images: Iterable[str] = (),
    audios: Iterable[str] = (),
    videos: Iterable[str] = (),
) -> Message:
    """构造 user 消息。媒体参数是公网 URL 或 ``data:<mime>;base64,...``。"""
    parts: list[dict[str, Any]] = [
        *({"type": "image_url", "image_url": {"url": url}} for url in images),
        *({"type": "input_audio", "input_audio": {"data": url}} for url in audios),
        *({"type": "video_url", "video_url": {"url": url}} for url in videos),
    ]
    if not parts:
        return {"role": "user", "content": text}
    if text:
        parts.append({"type": "text", "text": text})
    return {"role": "user", "content": parts}


def tool_result_message(tool_call_id: str, content: str) -> Message:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _assistant_message(raw: dict[str, Any]) -> Message:
    """从响应里取出要写回历史的 assistant 消息，丢掉 null 字段和仅响应侧的字段。"""
    message: Message = {"role": "assistant", "content": raw.get("content") or ""}
    if raw.get("tool_calls"):
        message["tool_calls"] = raw["tool_calls"]
    if raw.get("reasoning_content"):
        message["reasoning_content"] = raw["reasoning_content"]
    return message


def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw_calls or []:
        function = item.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args)
            valid = isinstance(parsed, dict)
        except (TypeError, ValueError):
            parsed, valid = None, False
        calls.append(
            ToolCall(
                id=str(item.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=parsed if valid else {},
                valid=valid,
            )
        )
    return calls


def _parse_usage(raw: dict[str, Any] | None) -> LLMUsage:
    raw = raw or {}
    return LLMUsage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        reasoning_tokens=int(
            (raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        ),
        cached_tokens=int((raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
    )


class LLMClient:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_base_seconds: float = 1.0,
    ):
        self.settings = settings
        #: 每调一次模型（成没成都算）通知一声，处理记录和用量统计靠它（见 core/trace.py）
        self.on_call: Callable[[LLMCall], None] | None = None
        self._retry_base_seconds = retry_base_seconds
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            # timeout_seconds 是等模型生成完的时限；连都连不上不用等那么久（连接失败会重试）
            timeout=httpx.Timeout(settings.timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
            headers={"api-key": settings.api_key, "Content-Type": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _profile(self, name: str | None) -> LLMProfile:
        key = name or self.settings.default_profile
        try:
            return self.settings.profiles[key]
        except KeyError:
            raise LLMError(f"未定义的 LLM profile：{key}") from None

    def _build_body(
        self,
        messages: Sequence[Message],
        profile: LLMProfile,
        *,
        tools: Sequence[dict[str, Any]] | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": profile.model,
            "messages": list(messages),
            "max_completion_tokens": profile.max_completion_tokens,
            # 默认值随模型版本变过，所以每次都显式发送
            "thinking": {"type": "enabled" if profile.thinking else "disabled"},
            "stream": False,
        }
        if profile.temperature is not None and not profile.thinking:
            body["temperature"] = profile.temperature

        if tools:
            body["tools"] = list(tools)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        attempts = self.settings.max_retries + 1
        last_error: LLMError | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._retry_base_seconds * 2 ** (attempt - 1))
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except httpx.ReadTimeout as exc:
                # 等满了时限还没生成完：重来一遍只会让用户再等这么久，不重试
                raise LLMError(
                    f"LLM 在 {self.settings.timeout_seconds:.0f} 秒内没有生成完", retryable=False
                ) from exc
            except httpx.HTTPError as exc:
                last_error = LLMError(f"LLM 请求失败：{exc!r}", retryable=True)
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    return data
                # 状态码是 200 但不是模型的回复（中间的网关塞了个错误页之类）：当成接口一时出错
                last_error = LLMError(
                    f"LLM 接口返回的不是 JSON 对象：{resp.text[:300]}", status=200, retryable=True
                )
                continue

            detail = resp.text[:500]
            if resp.status_code == 421:
                raise LLMContentBlocked(f"内容被审核拦截：{detail}", status=421)
            error = LLMError(
                f"LLM 接口返回 HTTP {resp.status_code}：{detail}",
                status=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUS,
            )
            if not error.retryable:
                raise error
            last_error = error
        assert last_error is not None
        raise last_error

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        profile: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> LLMResult:
        prof = self._profile(profile)
        body = self._build_body(
            messages,
            prof,
            tools=tools,
            json_mode=json_mode,
        )
        started_at = datetime.now(UTC)
        clock = time.perf_counter()

        def report(result: LLMResult | None, error: str = "") -> None:
            """这次调用的结果交给 ``on_call``（没人要就不整理）。"""
            if self.on_call is None:
                return
            used = result.usage if result is not None else LLMUsage()
            names = [
                str((t.get("function") or {}).get("name") or t.get("type")) for t in tools or []
            ]
            call = LLMCall(
                started_at=started_at,
                seconds=time.perf_counter() - clock,
                profile=prof.name,
                model=result.model if result is not None else prof.model,
                thinking=prof.thinking,
                ok=result is not None,
                finish_reason=result.finish_reason if result is not None else "",
                error=error,
                prompt_tokens=used.prompt_tokens,
                completion_tokens=used.completion_tokens,
                reasoning_tokens=used.reasoning_tokens,
                cached_tokens=used.cached_tokens,
                tool_calls=len(result.tool_calls) if result is not None else 0,
                request={
                    "messages": compact_messages(messages),
                    "tools": names,
                    "json_mode": json_mode,
                },
                response=None
                if result is None
                else {
                    "content": result.content,
                    "reasoning": result.reasoning,
                    "tool_calls": result.message.get("tool_calls") or [],
                },
            )
            try:
                self.on_call(call)
            except Exception:  # 记录出错不能让这次调用跟着失败
                logger.exception("llm_call_report_failed")

        try:
            data = await self._post(body)
            choices = data.get("choices") or []
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise LLMError(f"LLM 响应里没有 choices：{str(data)[:300]}")
            choice = choices[0]
            raw_message = choice.get("message") or {}
            if not isinstance(raw_message, dict):
                raise LLMError(f"LLM 响应里的 message 不是对象：{str(data)[:300]}")
            usage = _parse_usage(data.get("usage"))
            result = LLMResult(
                message=_assistant_message(raw_message),
                content=raw_message.get("content") or "",
                reasoning=raw_message.get("reasoning_content") or "",
                tool_calls=_parse_tool_calls(raw_message.get("tool_calls")),
                finish_reason=str(choice.get("finish_reason") or ""),
                usage=usage,
                model=str(data.get("model") or prof.model),
            )
        except asyncio.CancelledError:
            # 调用方给这次调用限了时，到点不等了（见查证工作流的几道限时）
            report(None, "调用方不等了：限时到了，或者处理被取消")
            raise
        except LLMError as exc:
            report(None, brief(exc, 500))
            raise
        report(result)
        logger.info(
            "llm_call",
            profile=prof.name,
            model=result.model,
            finish_reason=result.finish_reason,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cached_tokens=usage.cached_tokens,
            tool_calls=len(result.tool_calls),
        )
        return result

    async def chat_json(
        self,
        messages: Sequence[Message],
        schema: type[ModelT],
        *,
        profile: str | None = "extract",
        attempts: int = 2,
    ) -> ModelT:
        """JSON 模式调用，并按 ``schema`` 校验；校验失败时把错误回给模型重试。

        提示词里必须写明「只返回 JSON」以及字段结构，否则模型不会输出 JSON。
        """
        history = list(messages)
        problem = "未知错误"
        for _ in range(max(attempts, 1)):
            result = await self.chat(history, profile=profile, json_mode=True)
            if result.finish_reason == "length":
                problem = "输出被 max_completion_tokens 截断"
            else:
                try:
                    return schema.model_validate_json(result.content)
                except ValidationError as exc:
                    problem = exc.json(include_url=False, include_input=False)
            history = [
                *history,
                result.message,
                {
                    "role": "user",
                    "content": f"上一次输出不符合要求：{problem}。请只返回修正后的完整 JSON。",
                },
            ]
        raise LLMError(f"结构化输出在 {attempts} 次尝试后仍不合法：{problem}")
