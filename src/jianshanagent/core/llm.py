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
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

import httpx
from pydantic import BaseModel, ValidationError

from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger
from jianshanagent.core.trace import LLMCall, brief, compact_messages, current, current_step
from jianshanagent.core.usage import UsagePersistenceError, llm_usage, metered_post

logger = get_logger(__name__)

Message = dict[str, Any]
ModelT = TypeVar("ModelT", bound=BaseModel)
ResultT = TypeVar("ResultT")

_CONTENT_BLOCK_RETRIES = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
#: 连模型接口最多等这么久（等它生成完的时限是配置里的 timeout_seconds，长得多）
_CONNECT_TIMEOUT_SECONDS = 10.0


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMContentBlocked(LLMError):
    """平台拦截了输入或输出（HTTP 421，或 finish_reason 为 content_filter）。"""

    stage: str = ""


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


class _InvalidJSON(LLMError):
    def __init__(self, result: LLMResult, problem: str):
        super().__init__(problem)
        self.result = result


def _diagnostic_id(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", value):
        return value
    return None


@dataclass(slots=True)
class _ProviderMetadata:
    http_status: int | None = None
    provider_request_id: str | None = None
    provider_response_id: str | None = None
    finish_reason: str | None = None

    def capture(self, response: httpx.Response, data: Any) -> None:
        """只读取明确的关联字段，不保留响应头全集、审核错误详情或正文。"""
        self.http_status = response.status_code
        payload = data if isinstance(data, dict) else {}
        error = payload.get("error")
        self.provider_request_id = (
            _diagnostic_id(response.headers.get("x-request-id"))
            or _diagnostic_id(response.headers.get("request-id"))
            or _diagnostic_id(response.headers.get("x-mimo-request-id"))
            or _diagnostic_id(payload.get("request_id"))
            or _diagnostic_id(payload.get("requestId"))
            or (_diagnostic_id(error.get("request_id")) if isinstance(error, dict) else None)
        )
        self.provider_response_id = _diagnostic_id(payload.get("id"))
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            self.finish_reason = _diagnostic_id(choices[0].get("finish_reason"))


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

    async def _post(
        self,
        body: dict[str, Any],
        *,
        on_retry: Callable[[], None],
        allow_retry: bool,
        metadata: _ProviderMetadata,
    ) -> dict[str, Any]:
        attempts = self.settings.max_retries + 1 if allow_retry else 1
        last_error: LLMError | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._retry_base_seconds * 2 ** (attempt - 1))
                on_retry()
            try:
                resp = await metered_post(
                    self._client,
                    "/chat/completions",
                    body=body,
                    kind="llm",
                    model=body["model"],
                    read_usage=llm_usage,
                )
            except httpx.ReadTimeout as exc:
                # 等满了时限还没生成完：重来一遍只会让用户再等这么久，不重试
                raise LLMError(
                    f"LLM 在 {self.settings.timeout_seconds:.0f} 秒内没有生成完", retryable=False
                ) from exc
            except httpx.HTTPError as exc:
                last_error = LLMError(f"LLM 请求失败：{exc!r}", retryable=True)
                continue
            if resp.status_code in (200, 421):
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                metadata.capture(resp, data)
                if resp.status_code == 421:
                    raise LLMContentBlocked("内容被平台审核拦截", status=421)
                if isinstance(data, dict):
                    return data
                # 状态码是 200 但不是模型的回复（中间的网关塞了个错误页之类）：当成接口一时出错
                last_error = LLMError(
                    f"LLM 接口返回的不是 JSON 对象：{resp.text[:300]}", status=200, retryable=True
                )
                continue

            detail = resp.text[:500]
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

    async def _retry_content_block(
        self,
        send: Callable[[bool, bool, _ProviderMetadata], Awaitable[ResultT]],
        *,
        deadline: float | None,
        retrying: bool,
    ) -> ResultT:
        """各阶段共用一次最多三次的拦截重试预算；开始被拦后不可降级为普通错误。"""
        loop = asyncio.get_running_loop()
        retry_deadline = (
            deadline if deadline is not None else loop.time() + self.settings.timeout_seconds
        )
        blocked: LLMContentBlocked | None = None
        retry_group_id: str | None = None
        step, trace = current_step(), current()
        context = {"stage": step.kind if step else None, "trace_id": trace.id if trace else None}

        def diagnostic(event: str, metadata: _ProviderMetadata, attempt: int) -> None:
            logger.info(
                event,
                **context,
                retry_group_id=retry_group_id,
                attempt=attempt,
                http_status=metadata.http_status,
                finish_reason=metadata.finish_reason,
                provider_request_id=metadata.provider_request_id,
                provider_response_id=metadata.provider_response_id,
            )

        for attempt in range(_CONTENT_BLOCK_RETRIES + 1):
            limit = retry_deadline if attempt else deadline
            if limit is not None and loop.time() >= limit:
                if blocked is None:
                    raise TimeoutError
                break
            if attempt:
                logger.info(
                    "llm_content_block_retry",
                    **context,
                    retry_group_id=retry_group_id,
                    attempt=attempt,
                    limit=_CONTENT_BLOCK_RETRIES,
                )
            metadata = _ProviderMetadata()
            try:
                async with asyncio.timeout_at(limit):
                    result = await send(retrying or attempt > 0, attempt > 0, metadata)
            except LLMContentBlocked as exc:
                exc.stage = step.kind if step else ""
                blocked = exc
                if retry_group_id is None:
                    retry_group_id = uuid4().hex
                diagnostic("llm_content_blocked", metadata, attempt)
            except (LLMError, TimeoutError):
                if blocked is None:
                    raise
            else:
                if blocked is not None:
                    diagnostic("llm_content_block_retry_succeeded", metadata, attempt)
                return result
        assert blocked is not None
        raise blocked

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        profile: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        json_mode: bool = False,
        retrying: bool = False,
        deadline: float | None = None,
    ) -> LLMResult:
        """用原请求重试内容拦截。deadline 使用 asyncio 事件循环的单调时钟。"""

        async def send(resent: bool, block_retry: bool, metadata: _ProviderMetadata) -> LLMResult:
            return await self._chat_once(
                messages,
                metadata=metadata,
                profile=profile,
                tools=tools,
                json_mode=json_mode,
                retrying=resent,
                block_retry=block_retry,
            )

        return await self._retry_content_block(send, deadline=deadline, retrying=retrying)

    async def _chat_once(
        self,
        messages: Sequence[Message],
        *,
        metadata: _ProviderMetadata,
        profile: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        json_mode: bool = False,
        retrying: bool = False,
        block_retry: bool = False,
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
        retry_count = int(retrying)

        def count_retry() -> None:
            nonlocal retry_count
            retry_count += 1

        def report(result: LLMResult | None, error: str = "", *, finish_reason: str = "") -> None:
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
                ok=result is not None and not error,
                finish_reason=result.finish_reason if result is not None else finish_reason,
                error=error,
                prompt_tokens=used.prompt_tokens,
                completion_tokens=used.completion_tokens,
                reasoning_tokens=used.reasoning_tokens,
                cached_tokens=used.cached_tokens,
                tool_calls=len(result.tool_calls) if result is not None else 0,
                retry_count=retry_count,
                request={
                    "messages": compact_messages(messages),
                    "tools": names,
                    "json_mode": json_mode,
                },
                response=None
                if result is None or result.finish_reason == "content_filter"
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
            # 拦截重试每轮只发一次，不和传输层重试相乘。
            data = await self._post(
                body, on_retry=count_retry, allow_retry=not block_retry, metadata=metadata
            )
            choices = data.get("choices") or []
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise LLMError(f"LLM 响应里没有 choices：{str(data)[:300]}")
            choice = choices[0]
            finish_reason = str(choice.get("finish_reason") or "")
            raw_message = {} if finish_reason == "content_filter" else choice.get("message") or {}
            if not isinstance(raw_message, dict):
                raise LLMError(f"LLM 响应里的 message 不是对象：{str(data)[:300]}")
            usage = _parse_usage(data.get("usage"))
            result = LLMResult(
                message=_assistant_message(raw_message),
                content=raw_message.get("content") or "",
                reasoning=raw_message.get("reasoning_content") or "",
                tool_calls=_parse_tool_calls(raw_message.get("tool_calls")),
                finish_reason=finish_reason,
                usage=usage,
                model=str(data.get("model") or prof.model),
            )
        except asyncio.CancelledError:
            # 调用方给这次调用限了时，到点不等了（见查证工作流的几道限时）
            report(None, "调用方不等了：限时到了，或者处理被取消")
            raise
        except LLMContentBlocked as exc:
            report(None, str(exc), finish_reason="content_filter")
            raise
        except (LLMError, UsagePersistenceError) as exc:
            report(None, brief(exc, 500))
            raise
        if result.finish_reason == "content_filter":
            blocked = LLMContentBlocked("内容被平台审核拦截", status=200)
            report(result, str(blocked))
            raise blocked
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
        retrying: bool = False,
        deadline: float | None = None,
    ) -> ModelT:
        """校验 JSON。只有普通校验错误才反馈修格式；遇到内容拦截只原样重试。"""
        history = list(messages)

        async def send(resent: bool, block_retry: bool, metadata: _ProviderMetadata) -> ModelT:
            result = await self._chat_once(
                history,
                metadata=metadata,
                profile=profile,
                json_mode=True,
                retrying=resent,
                block_retry=block_retry,
            )
            if result.finish_reason == "length":
                raise _InvalidJSON(result, "输出被 max_completion_tokens 截断")
            try:
                return schema.model_validate_json(result.content)
            except ValidationError as exc:
                raise _InvalidJSON(
                    result, exc.json(include_url=False, include_input=False)
                ) from None

        problem = "未知错误"
        for attempt in range(max(attempts, 1)):
            try:
                return await self._retry_content_block(
                    send, deadline=deadline, retrying=retrying or attempt > 0
                )
            except _InvalidJSON as exc:
                problem = str(exc)
                history.extend(
                    [
                        exc.result.message,
                        user_message(
                            f"上一次输出不符合要求：{problem}。请只返回修正后的完整 JSON。"
                        ),
                    ]
                )
        raise LLMError(f"结构化输出在 {attempts} 次尝试后仍不合法：{problem}")
