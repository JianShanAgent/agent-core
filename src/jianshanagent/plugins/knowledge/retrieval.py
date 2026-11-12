"""检索：向量为主，关键词补位。

- 向量一路：把问题算成向量（和库里的向量必须是同一个指纹），按相似度排。它是主力：
  实测 60 个问题里 98% 的目标切片在它的前 10 名。
- 关键词一路：见 terms.py 和 store.py。它单独用不如向量（前 10 名里有 87%），
  但擅长向量不擅长的事：问题里有很少见的字眼（「最老的树根」、版本号）时，它能把那一片排到第一。
- 合并：关键词的前 ``keyword_slots`` 名一定给，剩下的名额按向量的名次补满 ``top_k`` 片。
  两路找到的是同一片时不重复占名额，所以向量至少占 ``top_k - keyword_slots`` 片。

为什么不按名次加权混排（RRF）：实测混排后前 10 名的命中率反而从 98% 掉到 95% ——
关键词一路里排得不上不下的切片会把向量排第 8、9 名的正确答案挤出去。

向量一路用不了的时候（嵌入接口出错或超时、没配密钥、库里的向量指纹和现在配置的不一致），
只用关键词一路，照样返回 ``top_k`` 片；``RetrievalResult.mode`` 会说明这次用了哪几路。

部署挂了几个 wiki 时，它们的切片在同一个库里，一起找、一起排，每片上标着出自哪个 wiki。
各个 wiki 的向量不是同一批算的（换嵌入型号换到一半）时，只和指纹对得上的那些比。

不做重排序：前 10 名直接交给大模型。

``search_pages`` 是给站内搜索那一级用的（见 research 插件）：站内全文搜索只能定位到页面，
页面里哪几片和问题有关，还是按向量在这些页面的切片里挑。

``fill_pages`` 把挑中的切片所在的页面补齐（见 :func:`plan_pages`）：一个页面入库时按长度切成了几片，
检索只挑最接近的几片，名单这类占好几片的内容就会缺一块，而模型看不出后面还有。
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from jianshanagent.core.embedding import EmbeddingClient, EmbeddingError, EmbeddingProvider
from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.store import ChunkPlace, Hit, KbChunk, KnowledgeStore
from jianshanagent.plugins.knowledge.terms import extract_terms

logger = get_logger(__name__)

MODE_HYBRID = "hybrid"
MODE_KEYWORD = "keyword"
MODE_VECTOR = "vector"
MODE_NONE = "none"

_VECTOR_CACHE = 64  # 记住最近这么多个问题的向量
#: 嵌入接口出错以后，这么久之内不再问它，只按关键词找。一个问题要算好几次问句向量
#: （点到名的每个条目、知识库、站内搜索各一次），接口卡住时每次都要等到超时再重试，加起来一分多钟
_EMBED_RETRY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    #: 最后交给大模型几片
    top_k: int = 10
    #: 其中留给关键词一路的名额（它的前几名一定给）
    keyword_slots: int = 2
    #: 出现在超过这个比例的切片里的词不参与关键词检索
    max_df_ratio: float = 0.2
    #: 算问题向量最多等多久、失败后再试几次（用户在等回复，不能像全量嵌入那样慢慢重试）
    embed_timeout_seconds: float = 8.0
    embed_retries: int = 1


@dataclass(frozen=True, slots=True)
class Ranked:
    chunk: KbChunk
    #: 在各路里的名次（从 1 数起）；那一路没找到它就是 None
    vector_rank: int | None = None
    keyword_rank: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    hits: list[Ranked]
    mode: str
    #: 向量一路没用上的原因（用上了就是 None）
    vector_skipped: str | None = None
    seconds: float = 0.0


def merge(
    vector_hits: list[Hit], keyword_hits: list[Hit], *, top_k: int, keyword_slots: int
) -> list[Ranked]:
    """关键词前 ``keyword_slots`` 名一定给，其余按向量名次补满；向量在前，关键词补进来的在后。

    向量一路是空的（用不了）时，全部名额都给关键词。
    """
    vector_rank = {hit.chunk.chunk_id: rank for rank, hit in enumerate(vector_hits, start=1)}
    keyword_rank = {hit.chunk.chunk_id: rank for rank, hit in enumerate(keyword_hits, start=1)}
    reserved = keyword_hits[: keyword_slots if vector_hits else top_k]
    chosen = {hit.chunk.chunk_id: hit.chunk for hit in reserved}
    for hit in vector_hits:
        if len(chosen) >= top_k:
            break
        chosen.setdefault(hit.chunk.chunk_id, hit.chunk)
    last = len(vector_hits) + len(keyword_hits) + 1
    order = sorted(
        chosen, key=lambda cid: (vector_rank.get(cid, last), keyword_rank.get(cid, last))
    )
    return [
        Ranked(
            chunk=chosen[chunk_id],
            vector_rank=vector_rank.get(chunk_id),
            keyword_rank=keyword_rank.get(chunk_id),
        )
        for chunk_id in order
    ]


@dataclass(frozen=True, slots=True)
class PagePlan:
    """把挑中的切片所在的页面补齐，该怎么补（:func:`plan_pages` 算出来的）。"""

    #: 要补进来的切片 id
    missing: list[str]
    #: 凑齐了的切片 → （它属于哪一整块，在页面里排第几）。一整块是一个页面，或者长页面里的一节；
    #: 挑中的和要补的都在里面，只有一片的页面也算
    whole: dict[str, tuple[str, int]]
    #: 挑中了、但所在的页面和所在的那一节都没凑齐的切片 id
    partial: frozenset[str]


def plan_pages(
    places: Sequence[ChunkPlace],
    picked: Sequence[str],
    *,
    min_hits: int,
    max_chars: int,
    room: int,
) -> PagePlan:
    """``places`` 是挑中的切片（``picked``）所在页面的全部切片，决定补哪些。

    - 一个页面挑中了 ``min_hits`` 片以上、整页不超过 ``max_chars`` 字：补成整页。
    - 整页太长：退一步按一级章节看，哪一节挑中了 ``min_hits`` 片以上、
      整节不超过 ``max_chars`` 字，就把那一节补成整节。
    - 一共只能多补 ``room`` 字；挑中得多的页面先补，放不下的整块不补（补一半没有意义）。
    - 其余挑中的切片算「节选」。本来就整页（整节）都在的不算。
    """
    rank = {chunk_id: n for n, chunk_id in enumerate(picked)}
    pages: dict[str, list[ChunkPlace]] = {}
    for place in places:
        pages.setdefault(place.doc_key, []).append(place)

    def hits(block: Sequence[ChunkPlace]) -> list[ChunkPlace]:
        return [p for p in block if p.chunk_id in rank]

    def fits(block: Sequence[ChunkPlace]) -> bool:
        got = len(hits(block))
        if got == len(block):
            return True
        return got >= min_hits and sum(p.chars for p in block) <= max_chars

    missing: list[str] = []
    whole: dict[str, tuple[str, int]] = {}
    partial: set[str] = set()
    order = sorted(
        (doc for doc in pages if hits(pages[doc])),
        key=lambda doc: (
            -len(hits(pages[doc])),
            min(rank[p.chunk_id] for p in hits(pages[doc])),
        ),
    )
    for doc in order:
        page = sorted(pages[doc], key=lambda p: p.seq)
        blocks: list[tuple[str, list[ChunkPlace]]] = []
        if fits(page):
            blocks.append((doc, page))
        else:
            sections: dict[str, list[ChunkPlace]] = {}
            for place in page:
                top = place.section_path[0] if place.section_path else ""
                sections.setdefault(top, []).append(place)
            blocks += [
                (f"{doc}\x1f{top}", section)
                for top, section in sections.items()
                if hits(section) and fits(section)
            ]
        for block_id, block in blocks:
            lacking = [p for p in block if p.chunk_id not in rank]
            cost = sum(p.chars for p in lacking)
            if cost > room:
                continue
            room -= cost
            missing += [p.chunk_id for p in lacking]
            whole.update({p.chunk_id: (block_id, p.seq) for p in block})
        partial.update(p.chunk_id for p in hits(page) if p.chunk_id not in whole)
    return PagePlan(missing=missing, whole=whole, partial=frozenset(partial))


def query_provider(provider: EmbeddingProvider, settings: RetrievalSettings) -> EmbeddingProvider:
    """算问题向量用的提供方配置：同一家、同一个指纹，只是超时和重试收紧。"""
    return dataclasses.replace(
        provider,
        timeout_seconds=settings.embed_timeout_seconds,
        max_retries=settings.embed_retries,
    )


def stitch_page(chunks: Sequence[KbChunk], *, chars: int) -> str:
    """把一个页面的切片（按页内先后）接回成一段正文，够 ``chars`` 字就不再往下接。

    每片开头那一行标题路径去掉；进到新的章节时补一行 Markdown 小标题
    （章节里面的小标题在切片正文里本来就是这种写法）。
    """
    parts: list[str] = []
    path: tuple[str, ...] = ()
    total = 0
    for chunk in chunks:
        head, _, body = chunk.text.partition("\n")
        if not head.startswith(chunk.title):  # 开头不是标题路径：整片都是正文
            body = chunk.text
        for depth, name in enumerate(chunk.section_path):
            if path[: depth + 1] != chunk.section_path[: depth + 1]:
                parts.append("#" * (depth + 2) + " " + name)
        path = chunk.section_path
        body = body.strip()
        if body:
            parts.append(body)
            total += len(body)
        if total >= chars:
            break
    return "\n\n".join(parts)


class Retriever:
    def __init__(
        self,
        store: KnowledgeStore,
        embedder: EmbeddingClient | None,
        settings: RetrievalSettings | None = None,
    ):
        self.store = store
        self.embedder = embedder
        self.settings = settings or RetrievalSettings()
        self._vectors: dict[str, Sequence[float]] = {}
        #: 嵌入接口刚出过错：到这个时刻（``time.monotonic``）之前不问它，原因记在 ``_embed_error``
        self._embed_off_until = 0.0
        self._embed_error = ""

    async def aclose(self) -> None:
        if self.embedder is not None:
            await self.embedder.aclose()

    @property
    def _fingerprint(self) -> str | None:
        """问题的向量是用哪个指纹算的：库里只有同一个指纹的向量能和它比。"""
        return self.embedder.provider.fingerprint if self.embedder is not None else None

    async def _query_vector(self, query: str) -> tuple[Sequence[float] | None, str | None]:
        """（问题的向量，没算成的原因）。同一个问题接连用到两次（先全库找、再在指定页面里找），只算一次。"""
        if self.embedder is None:
            return None, "没有配置嵌入接口"
        info = await self.store.info()
        fingerprint = self.embedder.provider.fingerprint
        if not info.has_vectors_of(fingerprint):
            have = {w.fingerprint for w in info.wikis.values() if w.with_vectors} or {None}
            stored = "、".join(str(item) for item in sorted(have, key=str))
            return None, f"库里向量的指纹是「{stored}」，现在配置的是「{fingerprint}」"
        cached = self._vectors.get(query)
        if cached is not None:
            return cached, None
        if time.monotonic() < self._embed_off_until:
            return None, f"嵌入接口刚出过错，这一阵先不用它：{self._embed_error}"
        try:
            vector = await self.embedder.embed_one(query)
        except EmbeddingError as exc:
            self._embed_error = str(exc)[:200]
            self._embed_off_until = time.monotonic() + _EMBED_RETRY_SECONDS
            return None, f"嵌入接口出错：{self._embed_error}"
        if len(self._vectors) >= _VECTOR_CACHE:
            self._vectors.pop(next(iter(self._vectors)))
        self._vectors[query] = vector
        return vector, None

    async def search_pages(
        self,
        query: str,
        titles: Sequence[str],
        *,
        limit: int,
        exclude: Collection[str] = (),
        wiki: str | None = None,
    ) -> list[KbChunk]:
        """只在给定标题的页面里，找和问题最接近的几片（向量用不了时按关键词挑）。

        站内搜索只能定位到页面，页面里哪几片有用还得靠这里挑。``exclude`` 是已经拿到过的切片 id。
        ``wiki``：这些标题是哪个 wiki 的页面（只在它的切片里挑）；不写就不分 wiki。
        """
        query = query.strip()
        if not query or not titles or limit <= 0:
            return []
        wanted = limit + len(exclude)
        vector, _skipped = await self._query_vector(query)
        if vector is not None:
            hits = await self.store.vector_search(
                vector, limit=wanted, titles=titles, wiki=wiki, fingerprint=self._fingerprint
            )
        else:
            hits = await self.store.keyword_search(
                extract_terms(query),
                limit=wanted,
                max_df_ratio=self.settings.max_df_ratio,
                titles=titles,
                wiki=wiki,
            )
        return [hit.chunk for hit in hits if hit.chunk.chunk_id not in exclude][:limit]

    async def page_text(self, title: str, *, chars: int, url_prefix: str = "") -> str:
        """知识库里标题是 ``title`` 的页面的正文，从头起约 ``chars`` 字；没有这一页返回空串。

        给 ``/查`` 用：有的页面是模板拼出来的，wiki 接口给不出正文摘要。不算向量，只查一次库。
        知识库不是实时的，给的是入库时的内容。``url_prefix`` 见 :meth:`KnowledgeStore.page`。
        """
        title = title.strip()
        if not title or chars <= 0:
            return ""
        return stitch_page(await self.store.page(title, url_prefix=url_prefix), chars=chars)

    async def fill_pages(
        self, picked: Sequence[str], *, room: int, min_hits: int = 2, max_chars: int = 8000
    ) -> tuple[PagePlan, list[KbChunk]]:
        """把已经挑中的切片（``picked`` 是它们的 id）所在的页面补齐：返回（怎么补，补进来的切片）。

        规则见 :func:`plan_pages`。不算向量，只查两次库。
        """
        places = await self.store.outline(picked)
        plan = plan_pages(places, picked, min_hits=min_hits, max_chars=max_chars, room=room)
        chunks = await self.store.get(plan.missing) if plan.missing else []
        return plan, chunks

    async def search(self, query: str, *, top_k: int | None = None) -> RetrievalResult:
        started = time.perf_counter()
        settings = self.settings
        limit = top_k or settings.top_k
        query = query.strip()
        if not query:
            return RetrievalResult(hits=[], mode=MODE_NONE)
        vector, skipped = await self._query_vector(query)
        vector_hits = (
            await self.store.vector_search(vector, limit=limit, fingerprint=self._fingerprint)
            if vector is not None
            else []
        )
        keyword_hits = await self.store.keyword_search(
            extract_terms(query), limit=limit, max_df_ratio=settings.max_df_ratio
        )
        hits = merge(
            vector_hits, keyword_hits, top_k=limit, keyword_slots=min(settings.keyword_slots, limit)
        )
        if vector_hits and keyword_hits:
            mode = MODE_HYBRID
        elif vector_hits:
            mode = MODE_VECTOR
        elif keyword_hits:
            mode = MODE_KEYWORD
        else:
            mode = MODE_NONE
        seconds = time.perf_counter() - started
        log = logger.warning if skipped else logger.info
        log(
            "kb_search",
            mode=mode,
            hits=len(hits),
            from_keywords_only=sum(hit.vector_rank is None for hit in hits),
            vector_skipped=skipped,
            seconds=round(seconds, 2),
            query_chars=len(query),
        )
        return RetrievalResult(hits=hits, mode=mode, vector_skipped=skipped, seconds=seconds)
