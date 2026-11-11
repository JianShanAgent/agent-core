"""取数阶段：决定哪些内容入库，并把原始内容取齐（命中缓存的不再请求）。

核心只管条目页：配置的命名空间里的非重定向页面，取渲染后的 HTML。

站点插件可以在两个地方插手（见 site.py）：

- 渲染之前（``select_pages``）：有的页面的内容完全来自别处（灰机的 Data 文档），渲染它等于把同一份
  内容再取一遍。插件把这样的页面认出来（:meth:`CollectContext.replace`），核心就不渲染它，
  由插件自己建的文档代替；
- 渲染之后（``collect``）：取插件自己要的原始内容，放进 :attr:`RawCorpus.extras`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.models import KIND_STORY
from jianshanagent.plugins.knowledge.settings import Settings
from jianshanagent.plugins.knowledge.sources import Fetched, Listing, PageRef, WikiSource

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Replaced:
    """一个不渲染的页面：它的内容由站点插件建的文档代替。"""

    ref: PageRef
    #: 代替它的是什么（文档标识，或者一句说明），记在这个页面的记录里
    by: str
    #: 代替它的文档是哪一类（``KIND_*``）
    kind: str = ""


@dataclass(slots=True)
class RawCorpus:
    """取齐的原始内容，交给处理阶段。"""

    listing: Listing
    #: 要渲染的页面：定位信息 + parse 接口的结果（或错误）
    rendered: list[Fetched] = field(default_factory=list)
    #: 配置里明确排除的页面
    excluded_pages: list[PageRef] = field(default_factory=list)
    #: 不渲染、由站点插件的文档代替的页面
    replaced_pages: list[Replaced] = field(default_factory=list)
    #: 各个站点插件自己取的原始内容：插件的名字 → 它自己定的结构
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def story_titles(self) -> frozenset[str]:
        """由剧情文档代替的页面的标题（认章节目录页时，指向它们的链接单独数）。"""
        return frozenset(item.ref.title for item in self.replaced_pages if item.kind == KIND_STORY)


@dataclass(slots=True)
class CollectContext:
    """取数阶段交给站点插件的东西。"""

    source: WikiSource
    settings: Settings
    corpus: RawCorpus
    #: 只处理这些标题（调试用）；None = 全部
    only_titles: set[str] | None = None
    #: 没被配置排除的条目页，按清单的顺序
    pages: list[PageRef] = field(default_factory=list)
    _replaced: set[int] = field(default_factory=set)

    @property
    def listing(self) -> Listing:
        return self.corpus.listing

    def wanted(self, ref: PageRef) -> bool:
        """这一份在不在这次要处理的范围里。``only_titles`` 里可以混着写条目标题和别的页面的标题。"""
        only = self.only_titles
        return only is None or ref.title in only or ref.data_id in only

    def replace(self, ref: PageRef, by: str, *, kind: str = "") -> None:
        """这个页面不渲染，由插件自己的文档代替。"""
        if ref.page_id not in self._replaced:
            self._replaced.add(ref.page_id)
            self.corpus.replaced_pages.append(Replaced(ref, by, kind))

    def is_replaced(self, ref: PageRef) -> bool:
        return ref.page_id in self._replaced


async def collect(
    source: WikiSource,
    settings: Settings,
    *,
    only_titles: set[str] | None = None,
) -> RawCorpus:
    """取齐全部（或指定标题的）原始内容。"""
    src = settings.sources
    listing = await source.listing()
    corpus = RawCorpus(listing=listing)
    ctx = CollectContext(source, settings, corpus, only_titles)
    for ref in listing.pages:
        if ref.title in src.exclude_titles:
            corpus.excluded_pages.append(ref)
        else:
            ctx.pages.append(ref)

    for site in settings.sites:
        await site.select_pages(ctx)

    to_render = [ref for ref in ctx.pages if not ctx.is_replaced(ref) and ctx.wanted(ref)]
    to_render.sort(key=lambda r: r.page_id)
    corpus.rendered = await source.fetch_many(to_render, source.parsed, label="页面")

    for site in settings.sites:
        await site.collect(ctx)
    return corpus


def corpus_summary(corpus: RawCorpus, settings: Settings) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "pages_listed": len(corpus.listing.pages),
        "pages_to_render": len(corpus.rendered),
        "pages_render_failed": sum(1 for f in corpus.rendered if f.error),
        "excluded_pages": len(corpus.excluded_pages),
        "replaced_pages": len(corpus.replaced_pages),
    }
    for site in settings.sites:
        summary |= site.summary(corpus)
    return summary
