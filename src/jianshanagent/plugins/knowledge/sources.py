"""选内容与取原始内容：列页面、取源码、取渲染后的 HTML。

这里只有 MediaWiki 自己的接口。站点插件另外要取的东西（灰机的 Data 文档）由插件自己取，
用的是这里的缓存和「取不下来就用旧的」那一套（:meth:`WikiSource.cached`、:meth:`WikiSource.older`）。

所有「取」都先查磁盘缓存（键：页面 ID + 修订号），命中就不发请求；
离线模式下连页面清单也用上次存下的快照。

页面的修订号不变，渲染结果也可能变。渲染缓存按下面几条判断过没过时（:meth:`WikiSource.parsed`，
都是 2026-11-11 在这个站上实测过的）：

- 它用到的模板、模块改了（或者有人手动刷新了页面）：页面的 ``touched`` 会变。清单接口本来就带着
  这个值，不用多发请求。缓存里记着取的时候 ``touched`` 是多少，对不上就重取。
- 它的内容来自页面以外的数据、那份数据改了（灰机的 Data 文档）：``touched`` 多半不变
  （实测 ``Data:Character/365.json`` 改了两次，「入内雀」的 ``touched`` 没动），而且 wiki 自己的
  渲染缓存有效期是一天，这一天里按修订号去取，拿到的还是旧的。这种页面由调用方点名
  （``fresh_titles``，哪些页面该点名由站点插件认），不按修订号取，改成把页面源码交给
  parse 接口现渲染一次——结果和按修订号取的一样（实测逐行一致），只是不经过 wiki 的缓存。
- 说不清是谁依赖了谁的（公共数据改了）：``refresh`` 全部重取，由自动更新定期做（见 sync.py）。

新的一版取不下来时（网络出错、请求数到了上限），缓存里有旧的就先用旧的，记进 ``stale``：
知识库里这一页内容旧一点，比整页消失强。下一次运行还会再取。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.fetcher import DiskCache, FetchError, WikiFetcher
from jianshanagent.plugins.knowledge.settings import Settings

logger = get_logger(__name__)

_LISTING_FILE = "listing.json"
_PARSE_PROPS = "text|sections|revid|displaytitle|categories"
#: 按源码现渲染时要的字段（渲染的是交过去的源码，不是某个修订，所以没有修订号）
_FRESH_PROPS = "text|sections|displaytitle|categories"
#: 按源码现渲染时源码是放在地址里发过去的，比这长的不这么取（这类页面的源码就一两行模板调用）
_FRESH_MAX_BYTES = 2000
#: 渲染缓存里记「怎么取的、取的时候页面的 touched 是多少」的键
META_KEY = "_kb"
#: wiki 自己的渲染缓存留多久（实测 parse 接口报的 Cache expiry 是 86400）：
#: Data 改了以后这么久之内，按修订号取到的可能还是改之前的渲染结果
WIKI_CACHE_SECONDS = 86400.0
#: 老的渲染缓存没有记 touched，只能拿缓存文件的时间和页面现在的 touched 比。
#: 两台机器的钟不一定对得上，touched 落在存盘前这么多秒以内的也当作变过，多取一次
_LEGACY_MARGIN_SECONDS = 600.0

# 重新渲染一个页面的原因
RENDER_NEW = "revision"  # 这个修订还没取过
RENDER_TOUCHED = "touched"  # 页面自己没改，它用到的模板、模块改了（或者被手动刷新过）
RENDER_DATA = "data"  # 它的内容来自的数据改了：按源码现渲染
RENDER_REFRESH = "refresh"  # 全部重取


def wiki_time(text: str) -> float | None:
    """接口里的时间（``2026-11-11T13:38:10Z``）→ 时间戳；认不出来返回 None。"""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PageRef:
    """一个 wiki 页面的定位信息（条目，或者站点插件要的别的命名空间的页面）。"""

    page_id: int
    title: str
    rev_id: int
    length: int = 0
    namespace: int = 0
    #: 页面的渲染结果最近一次失效的时间（接口的 ``touched``）；老的清单快照里没有，是空的
    touched: str = ""

    @property
    def data_id(self) -> str:
        """标题里的空格写成下划线的写法（地址里、灰机 Data 接口的 ``_id`` 里都是这种）。"""
        return self.title.replace(" ", "_")

    @property
    def basename(self) -> str:
        return self.data_id.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class ListingSpec:
    """站点插件除了条目页以外还要列的页面。"""

    #: 按前缀列：标题的前缀（不带命名空间，如 ``"Story/"``）→ 在哪个命名空间
    prefixes: dict[str, int] = field(default_factory=dict)
    #: 按标题点名（带命名空间的完整标题）
    titles: tuple[str, ...] = ()


@dataclass(slots=True)
class Listing:
    pages: list[PageRef]
    #: 站点插件按前缀要的：前缀（如 "微信/"）→ 页面清单
    data: dict[str, list[PageRef]]
    fetched_at: float = 0.0
    #: 站点插件按标题点名要的：标题 → 定位信息
    named: dict[str, PageRef] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "pages": [asdict(p) for p in self.pages],
            "data": {k: [asdict(p) for p in v] for k, v in self.data.items()},
            "named": {k: asdict(v) for k, v in self.named.items()},
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Listing:
        return cls(
            pages=[PageRef(**p) for p in raw.get("pages") or []],
            data={k: [PageRef(**p) for p in v] for k, v in (raw.get("data") or {}).items()},
            fetched_at=float(raw.get("fetched_at") or 0.0),
            named={k: PageRef(**v) for k, v in (raw.get("named") or {}).items()},
        )


@dataclass(slots=True)
class Fetched:
    """一次取数的结果：成功给 ``value``，失败给 ``error``（管线记入报告后继续）。"""

    ref: PageRef
    value: Any = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class Stale:
    """新的一版没取下来、拿缓存里的旧内容顶着的一份。"""

    title: str
    #: 哪一类内容：parse（页面的渲染结果），或者站点插件自己取的那一类（data = 灰机的 Data 文档）
    kind: str
    error: str


def _ref_from_api(page: dict[str, Any]) -> PageRef | None:
    if page.get("missing") or page.get("invalid") or "pageid" not in page:
        return None
    return PageRef(
        page_id=int(page["pageid"]),
        title=str(page["title"]),
        rev_id=int(page.get("lastrevid") or 0),
        length=int(page.get("length") or 0),
        namespace=int(page.get("ns") or 0),
        touched=str(page.get("touched") or ""),
    )


def single_template_call(wikitext: str) -> tuple[str, list[str]] | None:
    """源码整页只有一个模板调用时，返回（模板名，位置参数）；否则返回 None。

    例：``{{剧情页面|575|1}}`` → ``("剧情页面", ["575", "1"])``。
    """
    match = re.fullmatch(r"\s*\{\{\s*([^{}|]+?)\s*((?:\|[^{}|]*)*)\}\}\s*", wikitext or "")
    if not match:
        return None
    args = [a.strip() for a in match.group(2).split("|")[1:]]
    return match.group(1).replace("_", " "), args


class WikiSource:
    def __init__(
        self,
        fetcher: WikiFetcher | None,
        cache: DiskCache,
        settings: Settings,
        *,
        refresh: bool = False,
        reuse_listing: bool = False,
        fresh_titles: Collection[str] = (),
        refresh_since: float = 0.0,
    ):
        #: 为 None 表示离线模式：只读缓存，不发任何请求
        self.fetcher = fetcher
        self.cache = cache
        self.settings = settings
        #: 渲染结果全部重取（源码和 Data 文档由修订号唯一确定，不会过期，不受它影响）
        self.refresh = refresh
        #: 全部重取做到一半停了（请求数到了上限、wiki 出错）、下一次接着做时用：
        #: 渲染结果是这个时间以后取的页面已经重取过了，不用再取。0 = 没有这回事，全都重取
        self.refresh_since = refresh_since
        #: 沿用上次的页面清单，只补清单里缺的部分（新加的数据集）；页面和修订号都不重新列
        self.reuse_listing = reuse_listing
        #: 这些页面的内容来自的 Data 文档改了：不看缓存，按源码现渲染（见本文件顶部）
        self.fresh_titles = frozenset(fresh_titles)
        #: 这次重新渲染了几个页面，按原因分（``RENDER_*``）
        self.renders: Counter[str] = Counter()
        #: 新的一版没取下来、拿旧内容顶着的
        self.stale: list[Stale] = []

    @property
    def offline(self) -> bool:
        return self.fetcher is None

    def need_fetcher(self) -> WikiFetcher:
        if self.fetcher is None:
            raise FetchError("离线模式下缓存里没有这份内容")
        return self.fetcher

    def hit(self) -> None:
        if self.fetcher is not None:
            self.fetcher.stats.cache_hits += 1

    def cached(self, kind: str, ref: PageRef) -> Any | None:
        """缓存里这个页面这个修订的一份（``kind`` 是哪一类内容）；没有返回 None。"""
        value = self.cache.get(kind, ref.page_id, ref.rev_id)
        if value is not None:
            self.hit()
        return value

    def listing_spec(self) -> ListingSpec:
        """各个站点插件另外要列的页面，合在一起。"""
        prefixes: dict[str, int] = {}
        titles: list[str] = []
        for site in self.settings.sites:
            spec = site.listing()
            if spec is None:
                continue
            for prefix, namespace in spec.prefixes.items():
                prefixes.setdefault(prefix, namespace)
            titles += [title for title in spec.titles if title not in titles]
        return ListingSpec(prefixes, tuple(titles))

    # ── 清单 ──────────────────────────────────────────────────────────

    async def _all_pages(self, namespace: int, *, prefix: str = "") -> list[PageRef]:
        fetcher = self.need_fetcher()
        params = {
            "action": "query",
            "generator": "allpages",
            "gapnamespace": str(namespace),
            "gapfilterredir": "nonredirects",
            "gaplimit": "500",
            "prop": "info",
        }
        if prefix:
            params["gapprefix"] = prefix
        refs: dict[int, PageRef] = {}
        cont: dict[str, str] = {}
        while True:
            data = await fetcher.api("list", {**params, **cont})
            for page in (data.get("query") or {}).get("pages") or []:
                ref = _ref_from_api(page)
                if ref is not None:
                    refs[ref.page_id] = ref
            if "continue" not in data:
                break
            cont = {k: str(v) for k, v in data["continue"].items()}
        return sorted(refs.values(), key=lambda r: r.page_id)

    async def listing(self) -> Listing:
        """条目所在命名空间的非重定向页面，加上站点插件另外要的。离线时用上次的快照。"""
        raw = self.cache.get_named(_LISTING_FILE)
        if self.offline:
            if raw is None:
                raise FetchError("离线模式需要先联网跑过一次（缓存里没有页面清单）")
            return Listing.from_json(raw)
        src = self.settings.sources
        spec = self.listing_spec()
        prefixes = spec.prefixes
        wanted = list(spec.titles)
        if self.reuse_listing and raw is not None:
            # 沿用快照：页面清单和修订号都不动，只补快照里没有的前缀和标题
            listing = Listing.from_json(raw)
            missing_prefixes = [p for p in prefixes if p not in listing.data]
            missing_titles = [t for t in wanted if t not in listing.named]
            for prefix in missing_prefixes:
                listing.data[prefix] = await self._all_pages(prefixes[prefix], prefix=prefix)
            if missing_titles:
                by_title = {ref.title: ref for ref in await self.resolve_titles(missing_titles)}
                listing.named |= {t: by_title[t] for t in missing_titles if t in by_title}
            if missing_prefixes or missing_titles:
                self.cache.put_named(_LISTING_FILE, listing.to_json())
            return listing
        pages = await self._all_pages(src.namespace)
        data: dict[str, list[PageRef]] = {}
        for prefix, namespace in prefixes.items():
            data[prefix] = await self._all_pages(namespace, prefix=prefix)
        named: dict[str, PageRef] = {}
        if wanted:
            by_title = {ref.title: ref for ref in await self.resolve_titles(wanted)}
            named = {title: by_title[title] for title in wanted if title in by_title}
        listing = Listing(pages=pages, data=data, fetched_at=time.time(), named=named)
        self.cache.put_named(_LISTING_FILE, listing.to_json())
        return listing

    async def resolve_titles(self, titles: Sequence[str]) -> list[PageRef]:
        """按标题取定位信息。离线时从快照里找。"""
        if self.offline:
            listing = await self.listing()
            everything = [
                *listing.pages,
                *(r for refs in listing.data.values() for r in refs),
                *listing.named.values(),
            ]
            wanted = {t.replace("_", " ") for t in titles} | set(titles)
            return [r for r in everything if r.title in wanted or r.data_id in wanted]
        fetcher = self.need_fetcher()
        refs: list[PageRef] = []
        for start in range(0, len(titles), 50):
            data = await fetcher.api(
                "list",
                {"action": "query", "prop": "info", "titles": "|".join(titles[start : start + 50])},
            )
            for page in (data.get("query") or {}).get("pages") or []:
                ref = _ref_from_api(page)
                if ref is not None:
                    refs.append(ref)
        return refs

    # ── 页面源码（按源码现渲染时用；站点插件拿它认「整页只有一个模板调用」的页面）──────

    async def wikitext(self, refs: Sequence[PageRef]) -> dict[int, str]:
        result: dict[int, str] = {}
        missing: list[PageRef] = []
        for ref in refs:
            cached = self.cached("wikitext", ref)
            if cached is not None:
                result[ref.page_id] = str(cached.get("wikitext") or "")
            else:
                missing.append(ref)
        if not missing or self.offline:
            return result
        fetcher = self.need_fetcher()
        size = self.settings.fetch.wikitext_batch_size
        by_id = {ref.page_id: ref for ref in missing}
        for start in range(0, len(missing), size):
            batch = missing[start : start + size]
            params = {
                "action": "query",
                "prop": "revisions",
                "rvprop": "ids|content",
                "rvslots": "main",
                "pageids": "|".join(str(r.page_id) for r in batch),
            }
            cont: dict[str, str] = {}
            while True:  # 内容太大时接口会分几次给
                data = await fetcher.api("wikitext", {**params, **cont})
                for page in (data.get("query") or {}).get("pages") or []:
                    revisions = page.get("revisions") or []
                    if not revisions or "pageid" not in page:
                        continue
                    main = (revisions[0].get("slots") or {}).get("main") or {}
                    text = str(main.get("content") or "")
                    ref = by_id[int(page["pageid"])]
                    result[ref.page_id] = text
                    # 清单之后页面又被编辑时，这份源码属于更新的修订号；仍按清单里的修订号存，
                    # 下次清单变化后自然会重取
                    self.cache.put("wikitext", ref.page_id, ref.rev_id, {"wikitext": text})
                if "continue" not in data:
                    break
                cont = {k: str(v) for k, v in data["continue"].items()}
        return result

    # ── 渲染后的 HTML ─────────────────────────────────────────────────

    async def parsed(self, ref: PageRef) -> dict[str, Any]:
        """页面渲染后的结果（parse 接口返回的那一段）。什么时候用缓存、什么时候重取见本文件顶部。"""
        cached = self.cache.get("parse", ref.page_id, ref.rev_id)
        reason = RENDER_NEW if cached is None else self._outdated(ref, cached)
        if cached is not None and reason is None:
            self.hit()
            return dict(cached)
        try:
            parsed = await self._render(self.need_fetcher(), ref, fresh=reason == RENDER_DATA)
        except FetchError as exc:
            # 没取下来：这个修订以前取过就用那一份，没取过就用这个页面更早的修订
            older = cached if cached is not None else self.older("parse", ref)
            if older is None:
                raise
            self.stale.append(Stale(ref.title, "parse", str(exc)))
            return dict(older)
        self.cache.put("parse", ref.page_id, ref.rev_id, parsed)
        self.renders[reason or RENDER_NEW] += 1
        return parsed

    def older(self, kind: str, ref: PageRef) -> Any | None:
        """这个页面缓存过的更早的一版；离线时不用（离线就是照着清单快照原样重放）。

        新的一版取不下来时拿它顶着，顶了的要记进 :attr:`stale`。"""
        return None if self.offline else self.cache.latest(kind, ref.page_id)

    def _outdated(self, ref: PageRef, cached: dict[str, Any]) -> str | None:
        """缓存里这个修订的渲染结果要不要重取：返回原因，不用重取返回 None。"""
        if self.offline:
            return None
        if ref.title in self.fresh_titles:
            return RENDER_DATA
        raw = cached.get(META_KEY)
        meta: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if self.refresh:
            raw_at = meta.get("fetched_at")
            fetched_at = float(raw_at) if isinstance(raw_at, int | float) else None
            # 这一轮全部重取里已经取过的（上次做到一半停了）不用再取。刚按源码现渲染过的也不取：
            # 按修订号取到的可能是 wiki 缓存里更旧的那一份，会把刚拿到的新内容盖回去
            done = fetched_at is not None and 0 < self.refresh_since <= fetched_at
            fresher = (
                fetched_at is not None
                and meta.get("mode") == "text"
                and time.time() - fetched_at < WIKI_CACHE_SECONDS
            )
            if not (done or fresher):
                return RENDER_REFRESH
        if not ref.touched:
            return None  # 清单是老快照，没记 touched：只能按修订号认
        recorded = meta.get("touched")
        if recorded:
            return RENDER_TOUCHED if recorded != ref.touched else None
        # 老缓存没记取的时候 touched 是多少：看页面是不是在缓存存盘以后又失效过
        touched = wiki_time(ref.touched)
        stored = self.cache.stored_at("parse", ref.page_id, ref.rev_id)
        if touched is None or stored is None:
            return None
        return RENDER_TOUCHED if touched > stored - _LEGACY_MARGIN_SECONDS else None

    async def _render(self, fetcher: WikiFetcher, ref: PageRef, *, fresh: bool) -> dict[str, Any]:
        """向 wiki 要一次渲染结果。``fresh``：不按修订号取，把源码交过去现渲染。"""
        params = {
            "action": "parse",
            "oldid": str(ref.rev_id),
            "prop": _PARSE_PROPS,
            "disablelimitreport": "1",
            "disableeditsection": "1",
        }
        mode = "oldid"
        if fresh:
            text = (await self.wikitext([ref])).get(ref.page_id, "")
            if text and len(text.encode("utf-8")) <= _FRESH_MAX_BYTES:
                del params["oldid"]
                params |= {
                    "title": ref.title,
                    "text": text,
                    "contentmodel": "wikitext",
                    "prop": _FRESH_PROPS,
                }
                mode = "text"
            else:
                # 源码太长，放不进地址里：只能按修订号取，拿到的可能是 wiki 一天以内的缓存
                logger.warning("kb_fresh_render_too_long", title=ref.title, chars=len(text))
        data = await fetcher.api("parse", params)
        parsed = dict(data.get("parse") or {})
        if "text" not in parsed:
            raise FetchError("parse 接口没有返回 text")
        parsed.setdefault("revid", ref.rev_id)
        parsed[META_KEY] = {"touched": ref.touched, "fetched_at": time.time(), "mode": mode}
        return parsed

    # ── 页面的创建时间和最后编辑时间（推断日期用）──────────────────────────

    async def page_history(self, ref: PageRef) -> dict[str, str]:
        """``{"created_at", "last_edited_at"}``（ISO 时间）。按页面 ID + 修订号缓存。

        第一个修订的时间要 ``rvdir=newer&rvlimit=1``，这组参数一次只能查一个页面
        （实测批量会报 invalidparammix），所以每个页面两个请求；只有要推断日期的页面才查。
        """
        cached = self.cached("history", ref)
        if cached is not None:
            return dict(cached)
        fetcher = self.need_fetcher()
        base = {"action": "query", "prop": "revisions", "rvprop": "ids|timestamp", "rvlimit": "1"}
        out: dict[str, str] = {}
        for key, extra in (("created_at", {"rvdir": "newer"}), ("last_edited_at", {})):
            data = await fetcher.api("history", {**base, **extra, "pageids": str(ref.page_id)})
            pages = (data.get("query") or {}).get("pages") or []
            revisions = (pages[0].get("revisions") or []) if pages else []
            if revisions:
                out[key] = str(revisions[0].get("timestamp") or "")
        if not out.get("created_at"):
            raise FetchError("没有取到页面的第一个修订")
        self.cache.put("history", ref.page_id, ref.rev_id, out)
        return out

    # ── 批量 ──────────────────────────────────────────────────────────

    async def fetch_many(
        self,
        refs: Iterable[PageRef],
        getter: Callable[[PageRef], Awaitable[Any]],
        *,
        label: str,
    ) -> list[Fetched]:
        """并发取一批（并发和速率由 :class:`WikiFetcher` 控制）；单个失败不影响其它。"""
        refs = list(refs)
        done = 0

        async def one(ref: PageRef) -> Fetched:
            nonlocal done
            try:
                value = await getter(ref)
                out = Fetched(ref, value=value)
            except FetchError as exc:
                out = Fetched(ref, error=str(exc))
            done += 1
            if done % 100 == 0 or done == len(refs):
                logger.info("kb_fetch_progress", what=label, done=done, total=len(refs))
            return out

        return list(await asyncio.gather(*(one(ref) for ref in refs)))
