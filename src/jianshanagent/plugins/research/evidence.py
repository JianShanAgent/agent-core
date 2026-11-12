"""查证工作流里的「资料」，以及三级取证来源：知识库 → 站内搜索 → 联网搜索。

每一级拿回来的东西都整理成同一种形状（:class:`Evidence`），逐级累加，
一起交给查证员和复核员；回答后面的参考资料也从这里出。
用户发了图片时，以图搜图找到的相似图片也整理成资料（:func:`image_evidence`），排在最前面。

- 知识库：本地的向量 + 关键词检索（knowledge 插件），用用户的问题直接查。
  问题里点到名的条目，另外从它自己的页面里挑几片排在最前面（:class:`EntryPages`）。
- 站内搜索：查证员给出搜索词，拿去搜 wiki 的站内全文搜索，取前几个页面，
  再从这些页面的本地切片里挑和问题最接近的几片。它补的是嵌入检索没捞到的内容——
  站内搜索只能定位到页面、排序噪声也大，所以页面里用哪几片还是按向量挑。
- 联网搜索：用查证员给的搜索词在网上搜一次（core/websearch.py，按次计费），一个网页是一条资料。
  搜到的不是 wiki 的内容，只在前两级答不了时用。

每一级查完以后，同一个页面挑中了好几片的，把这一页剩下的也补上（:class:`PageFiller`）。

部署挂了不止一个 wiki 时，知识库里是几个 wiki 的内容，站内搜索也是几个 wiki 都搜。
这时每条资料标着出自哪个 wiki（``Evidence.source``），给模型看的清单和回答后面的参考资料里都带着；
只挂一个 wiki 时不标。
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any, Protocol

from jianshanagent.core.links import strip_unknown_links
from jianshanagent.core.websearch import WebPage
from jianshanagent.plugins.knowledge.plugin import TYPE_NAMES
from jianshanagent.plugins.knowledge.retrieval import Retriever
from jianshanagent.plugins.knowledge.store import KbChunk


@dataclass(frozen=True, slots=True)
class Evidence:
    #: 哪一级查到的，直接是给人看的名字（知识库 / 站内搜索 / 联网搜索）
    origin: str
    title: str
    url: str
    text: str
    #: 条目 / 公告 / 剧情 / 数据 / 网页
    kind: str = ""
    date: str = ""
    #: 去重用：知识库切片的 id，网页的链接
    key: str = ""
    #: 只是所在页面的一部分，页面上还有没给出来的内容（见 :class:`PageFiller`）
    excerpt: bool = False
    #: 出自哪个 wiki，给人看的叫法；只挂一个 wiki 时、以及不是 wiki 上的内容（网页）时为空
    source: str = ""


#: 各个 wiki 给人看的叫法：``core.wiki.yaml`` 里的 id → 叫法。只挂一个 wiki 时是空的（不标出处）
Sources = Mapping[str, str]
NO_SOURCES: Sources = {}


class EvidenceSource(Protocol):
    name: str
    #: 这一级的名字：标在它查到的资料上，也用来告诉查证员下一级是什么
    label: str
    #: 告诉查证员这一级的搜索词该怎么写
    keywords_hint: str

    async def collect(self, question: str, keywords: str, have: Collection[str]) -> list[Evidence]:
        """取证。``keywords`` 是查证员给的搜索词（第一级没有）；``have`` 是已有资料的 key。"""


#: 标在「只是页面一部分」的资料上的字（提示词里也用它）
EXCERPT = "节选"


def render(items: Sequence[Evidence]) -> str:
    """给模型看的资料清单，带编号（模型按编号引用）。"""
    blocks: list[str] = []
    for number, item in enumerate(items, start=1):
        head = "｜".join(
            part
            for part in (
                item.origin,
                f"出自 {item.source}" if item.source else "",
                item.kind,
                f"日期 {item.date}" if item.date else "",
                EXCERPT if item.excerpt else "",
            )
            if part
        )
        lines = [f"【资料 {number}】{head}"]
        if item.title and item.title not in item.text.split("\n", 1)[0]:
            lines.append(f"标题：{item.title}")
        if item.url:
            lines.append(f"链接：{item.url}")
        lines.append(item.text)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "（什么都没查到）"


def image_evidence(
    hits: Sequence[Any],
    *,
    origin: str,
    number: int,
    total: int,
    sources: Sources = NO_SOURCES,
) -> list[Evidence]:
    """以图搜图的结果 → 资料。``hits`` 是 knowledge 插件 ``ImageSearch.search`` 返回的相似图片；
    ``number`` / ``total``：这是用户发的第几张图、一共几张。"""
    which = f"第 {number} 张图片" if total > 1 else "图片"
    found: list[Evidence] = []
    for hit in hits:
        score = f"相似度 {float(hit.score):.2f}"
        if hit.sure:
            # 认准靠的是「比别的候选明显更像」，截图里的立绘相似度只有 0.7 上下也可能认准
            verdict = (
                f"程序比对后认定，用户发的{which}就是 wiki 上的这张图"
                f"（{score}，明显高于其它候选）。"
            )
        else:
            verdict = f"用户发的{which}和 wiki 上的这张图有些像（{score}），但不能确定是同一张。"
        lines = [verdict, f"wiki 上的这张图是：{hit.label}"]
        if hit.text:
            lines.append(str(hit.text))
        found.append(
            Evidence(
                origin=origin,
                title=str(hit.label),
                url=str(hit.url),
                text="\n".join(lines),
                kind=str(hit.kind),
                key=f"image:{hit.image_id}",
                source=sources.get(str(getattr(hit, "wiki", "")), ""),
            )
        )
    return found


def _from_chunk(chunk: KbChunk, origin: str, sources: Sources = NO_SOURCES) -> Evidence:
    date = chunk.date or (f"{chunk.date_inferred}（推断）" if chunk.date_inferred else "")
    return Evidence(
        origin=origin,
        title=chunk.title,
        url=chunk.url,
        text=chunk.text,
        kind=TYPE_NAMES.get(chunk.kind, chunk.kind),
        date=date,
        key=chunk.chunk_id,
        source=sources.get(chunk.wiki, ""),
    )


class KnowledgeSource:
    name = "kb"
    keywords_hint = ""

    def __init__(
        self, retriever: Retriever, *, label: str = "知识库", sources: Sources = NO_SOURCES
    ):
        self.retriever = retriever
        self.label = label
        self.sources = sources

    async def collect(self, question: str, keywords: str, have: Collection[str]) -> list[Evidence]:
        result = await self.retriever.search(question)
        return [_from_chunk(hit.chunk, self.label, self.sources) for hit in result.hits]


class EntryPages:
    """问题里点到名的条目（站点插件认得的条目、页面）自己的内容。

    分析问题时查到了名字对应的 wiki 条目（外号已经换成正式名），就从这些条目的页面里
    各挑几片和问题最接近的，排在资料的最前面——问「X 是什么」「X 的技能」时，
    X 自己的页面是最该先看的，光靠按意思检索容易先捞到别处提到 X 的内容。
    几个 wiki 都有这个条目的话，是从它们的页面里一起挑。
    """

    def __init__(
        self,
        retriever: Retriever,
        *,
        label: str = "知识库",
        per_name: int = 2,
        total: int = 4,
        sources: Sources = NO_SOURCES,
    ):
        self.retriever = retriever
        self.label = label
        self.sources = sources
        self.per_name = per_name
        self.total = total

    async def collect(
        self, question: str, titles: Sequence[str], have: Collection[str]
    ) -> list[Evidence]:
        found: list[Evidence] = []
        seen = set(have)
        for title in dict.fromkeys(titles):
            room = min(self.per_name, self.total - len(found))
            if room <= 0:
                break
            chunks = await self.retriever.search_pages(question, [title], limit=room, exclude=seen)
            seen.update(chunk.chunk_id for chunk in chunks)
            found += [_from_chunk(chunk, self.label, self.sources) for chunk in chunks]
        return found


class PageFiller:
    """同一个页面挑中了好几片时，把这一页剩下的也补上，让模型看到整页。

    页面入库时按长度切成了片，各级取证只挑和问题最接近的几片。名单这类占好几片的内容就会缺一块，
    而模型看不出后面还有——问「六星鬼王分别有哪些」，名单占 6 片，挑中 5 片，答出来就少一个。
    规则见 knowledge 插件的 ``plan_pages``：页面不长就补成整页，太长的只把挑中的那一节补成整节，
    还是放不下就不补，那几片标成「节选」，让模型知道页面上还有别的内容。
    """

    def __init__(
        self,
        retriever: Retriever,
        *,
        label: str = "知识库",
        min_hits: int = 2,
        max_chars: int = 8000,
        sources: Sources = NO_SOURCES,
    ):
        self.retriever = retriever
        self.label = label
        self.sources = sources
        self.min_hits = min_hits
        self.max_chars = max_chars

    async def fill(self, evidence: Sequence[Evidence], start: int, room: int) -> list[Evidence]:
        """``evidence[start:]`` 是这一级刚查到的，最多还能加 ``room`` 字；返回补齐以后的全部资料。

        ``start`` 之前的资料位置不动（查证员、复核员已经按编号引用过它们）。这一级的资料里，
        凑成了整页（或整节）的排在一起、按页面里的先后排，位置在其中最先查到的那一片那里。
        """
        keys = [item.key for item in evidence if item.key]
        plan, chunks = await self.retriever.fill_pages(
            keys, room=max(room, 0), min_hits=self.min_hits, max_chars=self.max_chars
        )

        def marked(item: Evidence) -> Evidence:
            excerpt = item.key in plan.partial
            return item if item.excerpt == excerpt else dataclasses.replace(item, excerpt=excerpt)

        old = [marked(item) for item in evidence[:start]]
        new = [marked(item) for item in evidence[start:]]
        #: 一整块（页面或章节）→ 这一级里属于它的资料和补进来的切片，带着页内次序
        blocks: dict[str, list[tuple[int, Evidence]]] = {}
        origins: dict[str, str] = {}
        for item in (*old, *new):
            if item.key in plan.whole:
                origins.setdefault(plan.whole[item.key][0], item.origin)
        for item in new:
            if item.key in plan.whole:
                block, seq = plan.whole[item.key]
                blocks.setdefault(block, []).append((seq, item))
        for chunk in chunks:
            block, seq = plan.whole[chunk.chunk_id]
            origin = origins.get(block, self.label)
            blocks.setdefault(block, []).append((seq, _from_chunk(chunk, origin, self.sources)))

        def in_page_order(block: str) -> list[Evidence]:
            return [item for _, item in sorted(blocks[block], key=lambda pair: pair[0])]

        result = old
        placed: set[str] = set()
        for item in new:
            block = plan.whole[item.key][0] if item.key in plan.whole else ""
            if not block:
                result.append(item)
            elif block not in placed:
                placed.add(block)
                result += in_page_order(block)
        for block in blocks:  # 挑中的那几片都在前几级里：补进来的排在最后
            if block not in placed:
                result += in_page_order(block)
        return result


#: 站内全文搜索：（搜索词，每个 wiki 最多几个页面）→ 各个 wiki 搜到的页面标题（wiki 的 id → 标题）
SiteSearch = Callable[[str, int], Awaitable[Mapping[str, Sequence[str]]]]


def site_keywords_hint(nouns: str = "", example: str = "") -> str:
    """告诉查证员站内搜索的搜索词怎么写。``nouns``：这个站优先用哪些专有名词（「式神名、活动名」）；
    ``example``：举一个例子。都因站而异，没有就不说。"""
    return (
        "站内全文搜索只认关键词：从问题里挑 1～3 个最能定位到页面的词，用空格隔开，"
        f"优先用专有名词{f'（{nouns}）' if nouns else ''}，口语称呼换成正式名；"
        "不要写整句，不要带「什么时候」「怎么」这类词。"
        f"{f'例：「{example}」' if example else ''}"
    )


class SiteSearchSource:
    name = "site"
    keywords_hint = site_keywords_hint()

    def __init__(
        self,
        retriever: Retriever,
        search: SiteSearch,
        *,
        label: str = "站内搜索",
        pages: int = 5,
        chunks: int = 6,
        timeout_seconds: float = 5.0,
        keywords_hint: str = "",
        sources: Sources = NO_SOURCES,
    ):
        if keywords_hint:
            self.keywords_hint = keywords_hint
        self.retriever = retriever
        self.search = search
        self.label = label
        self.sources = sources
        self.pages = pages
        self.chunks = chunks
        self.timeout_seconds = timeout_seconds

    async def collect(self, question: str, keywords: str, have: Collection[str]) -> list[Evidence]:
        if not keywords.strip():
            return []
        titles = await asyncio.wait_for(
            self.search(keywords.strip(), self.pages), self.timeout_seconds
        )
        # 每个 wiki 搜到的页面各自挑，再轮着排：一个 wiki 的页面不会把另一个的挤出去
        picked = [
            await self.retriever.search_pages(
                question, list(mine), limit=self.chunks, exclude=have, wiki=wiki
            )
            for wiki, mine in titles.items()
            if mine
        ]
        found = [chunk for turn in zip_longest(*picked) for chunk in turn if chunk is not None]
        return [_from_chunk(chunk, self.label, self.sources) for chunk in found[: self.chunks]]


#: 搜一次网页：搜索词 → 网页列表（``WebSearchClient.search``）
WebSearch = Callable[[str], Awaitable[list[WebPage]]]


class WebSearchSource:
    """联网搜索：用搜索词搜一次，一个网页是一条资料。接口出错由工作流当成这一级什么都没查到。

    网页的链接不给模型看，也不会出现在回复里：QQ 平台只让发报备过的域名的链接，
    带了别的域名的链接整条消息都发不出去（错误码 304003）。所以资料里只有标题、网站名和内容，
    内容里夹带的网址也去掉；链接只留着去重用。
    """

    name = "web"

    def __init__(self, search: WebSearch, *, label: str = "联网搜索", topic: str = ""):
        self.search = search
        self.label = label
        #: 搜索词里必须有的词（这个 wiki 讲的东西的名字）：没有就加在前面，不然搜到的多半是别的东西
        self.topic = topic.strip()
        about = f"带上「{self.topic}」和关键的名字" if self.topic else "带上关键的名字"
        self.keywords_hint = f"联网搜索的搜索词写成一句简短的搜索语，{about}。"

    async def collect(self, question: str, keywords: str, have: Collection[str]) -> list[Evidence]:
        query = keywords.strip() or question.strip()
        if self.topic and self.topic not in query:
            query = f"{self.topic} {query}"
        found: list[Evidence] = []
        for page in await self.search(query):
            if page.url in have:
                continue
            named = page.site and page.site not in page.title
            found.append(
                Evidence(
                    origin=self.label,
                    title=f"{page.title}（{page.site}）" if named else page.title or page.site,
                    url="",
                    text=strip_unknown_links(page.text, set()),
                    kind="网页",
                    date=page.date,
                    key=page.url,
                )
            )
        return found
