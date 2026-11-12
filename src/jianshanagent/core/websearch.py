"""联网搜索：给一句搜索词，拿回若干网页（标题、网站名、发布日期、内容摘要）。

只依赖 httpx。配置在 core.websearch.yaml：``provider`` 选用哪一家，各家的地址和密钥在
``providers`` 下。联网搜索有自己的连接、自己的密钥，对话模型（core/llm.py）不用它们。

``mimo``：小米 MiMo 对话接口里的 ``web_search`` 工具（``POST {base_url}/chat/completions``）。
搜到的网页在 ``choices[0].message.annotations`` 里（``url``、``title``、``summary``、``site_name``、
``publish_time``），用了几次搜索在 ``usage.web_search_usage`` 里；模型写的那句回答不用。
要按量付费的密钥，并在控制台开通「联网服务插件」——Token Plan 的密钥调用时报
``webSearchEnabled is false``。2026-11-12 实测：一次 5～8 秒；回答的 token 上限给到最小，
网页照样回来；同一个网页会在 ``annotations`` 里出现几次。

``gitee_ai``：模力方舟的「互联网搜索接口」（``POST {base_url}/web-search``）。2026-11-11 实测：
一次请求约 1.2 秒；打开 ``summary`` 时每个网页给 250～900 字的内容摘要；``siteName`` 有时是「无」。
响应是 ``{"code": 200, "data": {"webPages": {"value": [...]}}}``。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any

import httpx

from jianshanagent.core.clock import now_text
from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
PROVIDERS = ("mimo", "gitee_ai")
FRESHNESS = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
#: 接口在不知道网站名时填的字
_NO_SITE = {"", "无"}
#: MiMo 在账号没开通联网服务插件时的报错里有这个词
_MIMO_NOT_ENABLED = "webSearchEnabled"


class WebSearchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class WebSearchSettings:
    provider: str
    base_url: str
    api_key: str
    count: int = 6
    page_chars: int = 1200
    timeout_seconds: float = 10.0
    max_retries: int = 1
    # 以下几项只有 mimo 用
    model: str = ""
    force_search: bool = True
    max_keyword: int = 2
    max_completion_tokens: int = 16
    # 以下几项只有 gitee_ai 用
    summary: bool = True
    freshness: str = "noLimit"

    @classmethod
    def from_config(cls, config: AppConfig) -> WebSearchSettings | None:
        """读取 core.websearch.yaml 与密钥；没打开、或者密钥没填时返回 None。"""
        raw = config.get("core.websearch")
        if not raw.get("enabled", False):
            return None
        provider = str(raw.get("provider") or "")
        if provider not in PROVIDERS:
            known = "、".join(PROVIDERS)
            raise WebSearchError(
                f"core.websearch.yaml 的 provider「{provider}」不认识（可选：{known}）"
            )
        item = (raw.get("providers") or {}).get(provider) or {}
        base_url = str(item.get("base_url") or "").rstrip("/")
        if not base_url:
            raise WebSearchError(f"core.websearch.yaml 的 providers.{provider} 缺少 base_url")
        model = str(item.get("model") or "")
        if provider == "mimo" and not model:
            raise WebSearchError("core.websearch.yaml 的 providers.mimo 缺少 model")
        freshness = str(item.get("freshness") or "noLimit")
        if freshness not in FRESHNESS:
            known = "、".join(FRESHNESS)
            raise WebSearchError(
                f"core.websearch.yaml 的 freshness「{freshness}」不认识（可选：{known}）"
            )
        secret = str(item.get("secret") or "")
        api_key = str((config.secrets.get(secret) or {}).get("api_key") or "").strip()
        if not api_key:
            logger.warning("web_search_key_missing", provider=provider, secret=secret)
            return None
        retries = raw.get("max_retries")
        return cls(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            count=min(max(int(raw.get("count") or 6), 1), 50),
            page_chars=max(int(raw.get("page_chars") or 1200), 100),
            timeout_seconds=float(item.get("timeout_seconds") or 10),
            max_retries=int(retries) if retries is not None else 1,
            model=model,
            force_search=bool(item.get("force_search", True)),
            max_keyword=min(max(int(item.get("max_keyword") or 2), 1), 50),
            max_completion_tokens=max(int(item.get("max_completion_tokens") or 16), 16),
            summary=bool(item.get("summary", True)),
            freshness=freshness,
        )


@dataclass(frozen=True, slots=True)
class WebPage:
    title: str
    url: str
    #: 网页内容的摘要；接口没给摘要时是搜索结果里那一两句话
    text: str
    #: 网站的名字，接口不知道时是空的
    site: str = ""
    #: 发布日期（YYYY-MM-DD），接口没给时是空的
    date: str = ""


def _pages(rows: Iterable[tuple[Any, Any, Any, Any, Any]], page_chars: int) -> list[WebPage]:
    """（链接、标题、内容、网站名、日期）→ 网页列表：没有链接或没有内容的不要，同一个链接只留一条，
    内容太长的截断。"""
    found: dict[str, WebPage] = {}
    for raw_url, title, raw_text, raw_site, raw_date in rows:
        url = str(raw_url or "").strip()
        text = str(raw_text or "").strip()
        if not url.startswith("http") or not text or url in found:
            continue
        if len(text) > page_chars:
            text = text[:page_chars].rstrip() + "…"
        site = str(raw_site or "").strip()
        date = _DATE.match(str(raw_date or ""))
        found[url] = WebPage(
            title=str(title or "").strip(),
            url=url,
            text=text,
            site="" if site in _NO_SITE else site,
            date=date.group(0) if date else "",
        )
    return list(found.values())


def parse_pages(data: dict[str, Any], *, page_chars: int) -> list[WebPage]:
    """模力方舟的响应 → 网页列表。"""
    inner = data.get("data")
    pages = ((inner if isinstance(inner, dict) else data).get("webPages") or {}).get("value") or []
    return _pages(
        (
            (
                item.get("url"),
                item.get("name"),
                item.get("summary") or item.get("snippet"),
                item.get("siteName"),
                item.get("datePublished"),
            )
            for item in pages
            if isinstance(item, dict)
        ),
        page_chars,
    )


def parse_annotations(data: dict[str, Any], *, page_chars: int) -> list[WebPage]:
    """MiMo 的响应 → 网页列表：回复的 ``annotations`` 里类型是 ``url_citation`` 的那些。"""
    choices = data.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    annotations = (first.get("message") or {}).get("annotations") or []
    return _pages(
        (
            (
                item.get("url"),
                item.get("title"),
                item.get("summary"),
                item.get("site_name"),
                item.get("publish_time"),
            )
            for item in annotations
            if isinstance(item, dict) and item.get("type", "url_citation") == "url_citation"
        ),
        page_chars,
    )


class WebSearchClient:
    def __init__(
        self,
        settings: WebSearchSettings,
        *,
        timezone: tzinfo | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_base_seconds: float = 0.5,
    ):
        self.settings = settings
        self._timezone = timezone
        self._retry_base_seconds = retry_base_seconds
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.api_key}",
            },
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last_error: WebSearchError | None = None
        for attempt in range(self.settings.max_retries + 1):
            if attempt:
                await asyncio.sleep(self._retry_base_seconds * 2 ** (attempt - 1))
            try:
                resp = await self._client.post(path, json=body)
            except httpx.HTTPError as exc:
                last_error = WebSearchError(f"联网搜索请求失败：{exc!r}", retryable=True)
                continue
            if resp.status_code == 200:
                data = resp.json()
                if (
                    not isinstance(data, dict)
                    or data.get("code") not in (None, 200)
                    or data.get("error")
                ):
                    raise WebSearchError(f"联网搜索接口返回了错误：{resp.text[:300]}")
                return data
            if _MIMO_NOT_ENABLED in resp.text:
                raise WebSearchError(
                    "这个密钥的账号调不了 MiMo 的联网搜索：要用按量付费的密钥，并在控制台的"
                    f"「插件管理」里开通「联网服务插件」（接口原话：{resp.text[:200]}）",
                    status=resp.status_code,
                )
            error = WebSearchError(
                f"联网搜索接口返回 HTTP {resp.status_code}：{resp.text[:300]}",
                status=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUS,
            )
            if not error.retryable:
                raise error
            last_error = error
        assert last_error is not None
        raise last_error

    async def _search_mimo(self, query: str) -> tuple[list[WebPage], dict[str, Any]]:
        settings = self.settings
        data = await self._post(
            "/chat/completions",
            {
                "model": settings.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"现在是 {now_text(self._timezone)}。"
                            "用户给的是搜索词：联网搜索它，然后只用一句话说搜到了什么。"
                        ),
                    },
                    {"role": "user", "content": query},
                ],
                "tools": [
                    {
                        "type": "web_search",
                        "force_search": settings.force_search,
                        "max_keyword": settings.max_keyword,
                        "limit": settings.count,
                    }
                ],
                "tool_choice": "auto",
                "thinking": {"type": "disabled"},
                "max_completion_tokens": settings.max_completion_tokens,
                "stream": False,
            },
        )
        usage = data.get("usage") or {}
        searched = usage.get("web_search_usage") or {}
        return parse_annotations(data, page_chars=settings.page_chars), {
            "searches": searched.get("tool_usage"),
            "tokens": usage.get("total_tokens"),
        }

    async def _search_gitee_ai(self, query: str) -> tuple[list[WebPage], dict[str, Any]]:
        settings = self.settings
        data = await self._post(
            "/web-search",
            {
                "query": query,
                "count": settings.count,
                "summary": settings.summary,
                "freshness": settings.freshness,
            },
        )
        return parse_pages(data, page_chars=settings.page_chars), {}

    async def search(self, query: str) -> list[WebPage]:
        """搜一次（要花钱）。搜索词是空的就不搜。"""
        query = query.strip()
        if not query:
            return []
        started = time.perf_counter()
        if self.settings.provider == "mimo":
            pages, used = await self._search_mimo(query)
        else:
            pages, used = await self._search_gitee_ai(query)
        logger.info(
            "web_search",
            provider=self.settings.provider,
            results=len(pages),
            seconds=round(time.perf_counter() - started, 2),
            **used,
        )
        return pages
