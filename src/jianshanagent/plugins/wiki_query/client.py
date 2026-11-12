"""wiki 查询客户端：对任何 MediaWiki 都成立的查法，站点特有的条目由站点插件接进来。

一个客户端查一个 wiki。部署挂了几个 wiki 就有几个客户端，合在一起用的那一层在 hub.py
（命令、LLM 工具和查证工作流用的是它）。客户端的方法：

- :meth:`WikiQueryClient.search` 按名字找条目（先问站点插件，找不到再按标题、全文搜索）
- :meth:`WikiQueryClient.get_page` 取内容（站点插件认得的条目给结构化数据，普通页面给摘要）
- :meth:`WikiQueryClient.lookup_name` 查一个名字是谁的别名（站点插件登记的别名，页面的重定向）
- :meth:`WikiQueryClient.fulltext_titles` 站内全文搜索，只要页面标题

通用的部分只用 ``api.php``（都是只读请求，站点要放行请求头的话统一带上）：

- 普通页面取的是全文摘要（``prop=extracts``）；页面有没有、重定向到哪，
  用 ``titles`` + ``redirects`` 查。
- 全文搜索走 ``list=search``。装了 CirrusSearch 的站，它和网页上的搜索框是同一个搜索引擎、
  搜到的页面也一样，但接口默认的排法不一样：接口把被很多页面链接到的页面往前提，网页上不这样。
  排法由 ``search_ranking`` 定（接口的 ``srqiprofile``），见 plugin.wiki_query。

站点插件（:class:`~jianshanagent.plugins.wiki_query.provider.WikiProvider`）认得这个站自己的条目种类——
阴阳师 wiki 的式神、御魂是从灰机的 Data 命名空间里读的，见 ``jianshanagent_site_huiji_yys.wiki``。
它们发请求、放缓存都走这里（:meth:`WikiQueryClient.get_json`、:meth:`WikiQueryClient.remember`），
所以放行请求头、超时、缓存时间是同一套。
"""

from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from jianshanagent.core.logging import get_logger
from jianshanagent.core.wiki import WikiSite

if TYPE_CHECKING:
    from jianshanagent.plugins.wiki_query.provider import WikiProvider

logger = get_logger(__name__)

#: 普通页面。站点插件认得的条目种类由插件自己起代号（见 ``WikiProvider.kinds``）
KIND_PAGE = "page"

#: 配置的搜索排法被接口拒绝以后，这么久之内不再带它（免得每搜一次都先白发一个注定失败的请求）
_RANKING_RETRY_SECONDS = 600.0

#: 缓存里攒到这么多条时，每次写入顺手把过期的清掉
_CACHE_SWEEP_SIZE = 200

_HEADING_RE = re.compile(r"^=+\s*(.*?)\s*=+$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    kind: str = KIND_PAGE
    #: 查询词与正式名或别名完全一致
    exact: bool = False
    #: 出自哪个 wiki（``core.wiki.yaml`` 里的 id）。站点插件不用填，客户端会标上
    wiki: str = ""


@dataclass(frozen=True, slots=True)
class NameMatch:
    """一个名字在 wiki 数据里对上的条目。"""

    #: 正式名（站点插件认得的条目的名字，或页面标题）
    title: str
    kind: str = KIND_PAGE
    #: wiki 数据里登记的别名（昵称）
    aliases: tuple[str, ...] = ()
    #: 查的名字就是正式名、登记过的别名或重定向；False 表示只是正式名 / 别名里包含它
    exact: bool = True
    #: 出自哪个 wiki（``core.wiki.yaml`` 里的 id）。站点插件不用填，客户端会标上
    wiki: str = ""


@dataclass(slots=True)
class PageContent:
    title: str
    kind: str = KIND_PAGE
    url: str = ""
    #: 普通页面的文字摘要；站点插件的条目为空，内容在 data 里
    extract: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    #: 出自哪个 wiki（``core.wiki.yaml`` 里的 id）。站点插件不用填，客户端会标上
    wiki: str = ""


class WikiError(RuntimeError):
    """wiki 请求失败（被拦截、接口报错等）。"""


class WikiApiError(WikiError):
    """请求送到了，``api.php`` 明说了不行（参数不认识、搜索后端出错等）。"""


class _TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._store.get(key)
        if not item:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        now = time.monotonic()
        if len(self._store) >= _CACHE_SWEEP_SIZE:
            # 过期的条目只有再查同一个键时才会删；搜索词每次都不一样，不清的话只增不减
            for stale in [k for k, (expires, _) in self._store.items() if expires < now]:
                del self._store[stale]
        self._store[key] = (now + ttl, value)


class WikiQueryClient:
    def __init__(
        self,
        wiki: WikiSite,
        *,
        timeout_seconds: float = 12.0,
        cache_ttl_seconds: float = 300.0,
        search_ranking: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.wiki = wiki
        self.api_url = wiki.api_url
        self.site_base = wiki.site_base
        #: 全文搜索的结果按什么排（接口的 ``srqiprofile``）；空 = 接口默认的排法
        self.search_ranking = search_ranking
        #: 配置的排法被接口拒绝过：到这个时刻（``time.monotonic``）之前先不带它
        self._ranking_off_until = 0.0
        self.cache_ttl = cache_ttl_seconds
        self._cache = _TTLCache()
        #: 站点插件，按加载顺序
        self.providers: list[WikiProvider] = []
        headers = {"User-Agent": wiki.user_agent, "Accept": "application/json"}
        if wiki.auth_header and wiki.auth_key:
            # 站点在 Cloudflare 后面时的放行请求头（灰机是 X-authkey）
            headers[wiki.auth_header] = wiki.auth_key
        if wiki.cookie:
            headers["Cookie"] = wiki.cookie
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers=headers,
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def page_url(self, title: str) -> str:
        return self.wiki.page_url(title)

    # ── 站点插件 ──────────────────────────────────────────────────────

    def add_provider(self, provider: WikiProvider) -> None:
        self.providers.append(provider)

    def provider(self, provider_id: str) -> WikiProvider | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    @property
    def kind_labels(self) -> dict[str, str]:
        """站点插件认得的条目种类：代号 → 给人看的名字（``character`` → 式神），按加载顺序。"""
        labels: dict[str, str] = {}
        for provider in self.providers:
            for kind, label in provider.kinds.items():
                labels.setdefault(kind, label)
        return labels

    def kind_label(self, kind: str, default: str = "页面") -> str:
        return self.kind_labels.get(kind, default)

    # ── 传输 ──────────────────────────────────────────────────────────

    async def get_json(self, url: str, params: dict[str, str], *, error_key: str = "") -> Any:
        """取 JSON，成功的结果缓存一阵。站点插件读自己的接口也走这里。

        ``error_key``：接口用 HTTP 200 加这个键报错时（``api.php`` 是 ``error``），结果照样返回，
        但不进缓存——不然一次临时的故障会让同一个请求接下来几分钟一直失败。
        """
        cache_key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise WikiError(f"wiki 请求失败：{exc!r}") from exc
        if "json" not in resp.headers.get("content-type", ""):
            # 没带对放行请求头时，Cloudflare 返回的是 HTML 挑战页
            snippet = resp.text[:120].replace("\n", " ")
            raise WikiError(f"wiki 返回非 JSON（HTTP {resp.status_code}）：{snippet}")
        if resp.status_code >= 400:
            raise WikiError(f"wiki 接口报错（HTTP {resp.status_code}）：{resp.text[:200]}")
        data = resp.json()
        failed = bool(error_key) and isinstance(data, dict) and error_key in data
        if self.cache_ttl > 0 and not failed:
            self._cache.set(cache_key, data, self.cache_ttl)
        return data

    async def api(self, params: dict[str, str]) -> dict[str, Any]:
        """调 ``api.php``；接口层面的 error 抛 :class:`WikiApiError`。"""
        data = await self.get_json(
            self.api_url, {"format": "json", "formatversion": "2", **params}, error_key="error"
        )
        if "error" in data:
            raise WikiApiError(f"wiki API 错误：{data['error'].get('info') or data['error']}")
        return dict(data)

    def recall(self, key: str) -> Any | None:
        """站点插件放在缓存里的对象（解析好的索引之类）；没有或过期了返回 None。"""
        return self._cache.get(f"obj:{key}")

    def remember(self, key: str, value: Any) -> None:
        """把解析好的对象缓存一阵（和请求结果同样久），避免每次重建。"""
        if self.cache_ttl > 0:
            self._cache.set(f"obj:{key}", value, self.cache_ttl)

    # ── 对外的方法 ────────────────────────────────────────────────────

    async def search(self, q: str, *, limit: int = 5) -> list[SearchHit]:
        query = q.strip()
        if not query:
            return []
        hits: list[SearchHit] = []
        for provider in self.providers:
            hits += await provider.search(query)
        if not hits:
            # 普通页面：先按标题找；标题里没有，才退到全文搜索
            # （全文搜索命中的是「提到这个词的页面」，不一定是同名条目）
            folded = query.casefold()
            quoted = query.replace('"', " ")
            titles = await self._search_titles(f'intitle:"{quoted}"', limit)
            titles = titles or await self._search_titles(query, limit)
            hits += [SearchHit(t, KIND_PAGE, exact=t.casefold() == folded) for t in titles]

        hits.sort(key=lambda h: not h.exact)  # 稳定排序：完全一致的排前面
        return [self._mine(hit) for hit in hits[:limit]]

    def _mine(self, found: Any) -> Any:
        """给查到的东西标上出自这个 wiki。"""
        return dataclasses.replace(found, wiki=self.wiki.id) if self.wiki.id else found

    async def lookup_name(self, name: str, *, candidates: int = 3) -> list[NameMatch]:
        """查一个名字（可能是外号）对应哪个条目，用来把用户的叫法换成 wiki 上的正式名。

        依次看：站点插件登记的正式名、别名（完全一致）；都没有，看有没有这个标题的页面
        （含重定向——wiki 上的重定向就是页面的别名）；还没有，才给站点插件里「名字里包含它」的条目
        当候选（最多 ``candidates`` 个，``exact`` 为 False，只能当提示，不能当定论）。
        什么都对不上返回空列表。
        """
        query = name.strip()
        if not query:
            return []
        found: list[NameMatch] = []
        for provider in self.providers:
            found += await provider.lookup_name(query)
        if found:
            return [self._mine(match) for match in found]

        title = await self._resolve_title(query)
        if title is not None:
            return [self._mine(NameMatch(title, KIND_PAGE))]
        if len(query) < 2:  # 一个字能「包含」上一大片，没有意义
            return []
        partial: list[NameMatch] = []
        for provider in self.providers:
            partial += await provider.name_candidates(query, candidates - len(partial))
            if len(partial) >= candidates:
                break
        return [self._mine(match) for match in partial[:candidates]]

    async def _resolve_title(self, title: str) -> str | None:
        """有这个标题的页面就返回它的规范标题（跟随重定向）；没有返回 None。"""
        data = await self.api({"action": "query", "titles": title, "redirects": "1"})
        pages = data.get("query", {}).get("pages") or []
        if not pages or pages[0].get("missing") or pages[0].get("invalid"):
            return None
        return str(pages[0].get("title") or "") or None

    async def fulltext_titles(self, keywords: str, *, limit: int = 5) -> list[str]:
        """站内全文搜索，返回命中的页面标题（按站内的排序）。给知识库检索当一路证据来源用。"""
        query = keywords.strip()
        return await self._search_titles(query, limit) if query else []

    async def _search_titles(self, srsearch: str, limit: int) -> list[str]:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": srsearch,
            "srlimit": str(limit),
            "srprop": "",
        }
        ranked: dict[str, str] = {}
        if self.search_ranking and time.monotonic() >= self._ranking_off_until:
            ranked = {"srqiprofile": self.search_ranking}
        try:
            data = await self.api({**params, **ranked})
        except WikiApiError as exc:
            if not ranked:
                raise
            # 排法是这个站自己配的，站点升级后可能就没有了：不能因为它让搜索整个用不了。
            # 接下来一阵子不带它，过后再试（站点恢复了就又用上）
            self._ranking_off_until = time.monotonic() + _RANKING_RETRY_SECONDS
            logger.warning(
                "wiki_search_ranking_failed", ranking=self.search_ranking, error=str(exc)[:200]
            )
            data = await self.api(params)
        return [
            str(i["title"]) for i in data.get("query", {}).get("search") or [] if i.get("title")
        ]

    async def get_page(self, title: str, *, kind: str | None = None) -> PageContent | None:
        """取条目内容。``kind`` 来自 :class:`SearchHit`；不传则先挨个问站点插件，再当普通页面取。"""
        title = title.strip()
        if not title:
            return None
        page = await self._get_page(title, kind)
        if page is not None:
            page.wiki = self.wiki.id
        return page

    async def _get_page(self, title: str, kind: str | None) -> PageContent | None:
        for provider in self.providers:
            if kind is None or kind in provider.kinds:
                page = await provider.get_page(title, kind)
                if page is not None or kind is not None:
                    return page
        return await self._wiki_page(title)

    async def _wiki_page(self, title: str, *, extract_chars: int = 600) -> PageContent | None:
        data = await self.api(
            {
                "action": "query",
                "prop": "extracts|info",
                "explaintext": "1",
                "exchars": str(extract_chars),
                "titles": title,
                "inprop": "url",
                "redirects": "1",
            }
        )
        pages = data.get("query", {}).get("pages") or []
        if not pages or pages[0].get("missing") or pages[0].get("invalid"):
            return None
        page = pages[0]
        resolved = str(page.get("title") or title)
        extract = _HEADING_RE.sub(r"【\1】", str(page.get("extract") or ""))
        extract = "\n".join(line.strip() for line in extract.splitlines() if line.strip())
        return PageContent(
            title=resolved,
            kind=KIND_PAGE,
            url=str(page.get("fullurl") or self.page_url(resolved)),
            extract=extract,
        )
