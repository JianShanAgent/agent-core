"""联网搜索：模力方舟的「互联网搜索接口」（``POST {base_url}/web-search``）。

给一句搜索词，拿回若干网页：标题、网站名、发布日期、内容摘要。按次计费，和要几条无关。
只依赖 httpx。配置在 core.websearch.yaml，密钥和嵌入用的是同一个。

2026-11-11 实测：一次请求约 1.2 秒；打开 ``summary`` 时每个网页给 250～900 字的内容摘要；
``siteName`` 有时是「无」。响应是 ``{"code": 200, "data": {"webPages": {"value": [...]}}}``。

MiMo 自带的联网搜索没有用：它要另开「联网服务插件」，Token Plan 的密钥调用时报
``webSearchEnabled is false``。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
FRESHNESS = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
#: 接口在不知道网站名时填的字
_NO_SITE = {"", "无"}


class WebSearchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class WebSearchSettings:
    base_url: str
    api_key: str
    count: int = 6
    summary: bool = True
    freshness: str = "noLimit"
    page_chars: int = 1200
    timeout_seconds: float = 10.0
    max_retries: int = 1

    @classmethod
    def from_config(cls, config: AppConfig) -> WebSearchSettings | None:
        """读取 core.websearch.yaml 与密钥；没打开、或者密钥没填时返回 None。"""
        raw = config.get("core.websearch")
        if not raw.get("enabled", False):
            return None
        base_url = str(raw.get("base_url") or "").rstrip("/")
        if not base_url:
            raise WebSearchError("core.websearch.yaml 缺少 base_url")
        freshness = str(raw.get("freshness") or "noLimit")
        if freshness not in FRESHNESS:
            known = "、".join(FRESHNESS)
            raise WebSearchError(
                f"core.websearch.yaml 的 freshness「{freshness}」不认识（可选：{known}）"
            )
        secret = str(raw.get("secret") or "")
        api_key = str((config.secrets.get(secret) or {}).get("api_key") or "").strip()
        if not api_key:
            logger.warning("web_search_key_missing", secret=secret)
            return None
        retries = raw.get("max_retries")
        return cls(
            base_url=base_url,
            api_key=api_key,
            count=min(max(int(raw.get("count") or 6), 1), 50),
            summary=bool(raw.get("summary", True)),
            freshness=freshness,
            page_chars=max(int(raw.get("page_chars") or 1200), 100),
            timeout_seconds=float(raw.get("timeout_seconds") or 10),
            max_retries=int(retries) if retries is not None else 1,
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


def parse_pages(data: dict[str, Any], *, page_chars: int) -> list[WebPage]:
    """接口的响应 → 网页列表：没有链接或没有内容的不要，同一个链接只留一条，摘要太长的截断。"""
    inner = data.get("data")
    pages = ((inner if isinstance(inner, dict) else data).get("webPages") or {}).get("value") or []
    found: dict[str, WebPage] = {}
    for item in pages:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        text = str(item.get("summary") or item.get("snippet") or "").strip()
        if not url.startswith("http") or not text or url in found:
            continue
        if len(text) > page_chars:
            text = text[:page_chars].rstrip() + "…"
        site = str(item.get("siteName") or "").strip()
        date = _DATE.match(str(item.get("datePublished") or ""))
        found[url] = WebPage(
            title=str(item.get("name") or "").strip(),
            url=url,
            text=text,
            site="" if site in _NO_SITE else site,
            date=date.group(0) if date else "",
        )
    return list(found.values())


class WebSearchClient:
    def __init__(
        self,
        settings: WebSearchSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_base_seconds: float = 0.5,
    ):
        self.settings = settings
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

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last_error: WebSearchError | None = None
        for attempt in range(self.settings.max_retries + 1):
            if attempt:
                await asyncio.sleep(self._retry_base_seconds * 2 ** (attempt - 1))
            try:
                resp = await self._client.post("/web-search", json=body)
            except httpx.HTTPError as exc:
                last_error = WebSearchError(f"联网搜索请求失败：{exc!r}", retryable=True)
                continue
            if resp.status_code == 200:
                data = resp.json()
                code = data.get("code") if isinstance(data, dict) else None
                if not isinstance(data, dict) or code not in (None, 200):
                    raise WebSearchError(f"联网搜索接口返回了错误：{resp.text[:300]}")
                return data
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

    async def search(self, query: str) -> list[WebPage]:
        """搜一次（计一次费）。搜索词是空的就不搜。"""
        query = query.strip()
        if not query:
            return []
        settings = self.settings
        started = time.perf_counter()
        data = await self._post(
            {
                "query": query,
                "count": settings.count,
                "summary": settings.summary,
                "freshness": settings.freshness,
            }
        )
        pages = parse_pages(data, page_chars=settings.page_chars)
        logger.info(
            "web_search", results=len(pages), seconds=round(time.perf_counter() - started, 2)
        )
        return pages
