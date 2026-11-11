"""管线编排：取数 → 建文档（清洗、转 Markdown）→ 定日期 → 切片 → 去重 → 输出 → 报告。

核心只建一类文档：条目（渲染的页面，含章节目录页）。公告、剧情、由数据集生成的文档这些
是站点插件建的；切片以后给切片标实体也是插件的事（见 site.py）。

断点续跑靠的是原始内容缓存：耗时的只有取数（要限速），每取到一份就落盘，
中断后重跑会跳过已取到的部分。清洗和切片是纯计算，全量也只要几十秒，每次都整体重算，
输出文件先写临时文件、最后原子替换，所以不会出现「半份结果」。
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document, sha256_hex
from jianshanagent.plugins.knowledge.collect import RawCorpus, collect, corpus_summary
from jianshanagent.plugins.knowledge.dates import AnnouncementRef, PageHistory, month_days
from jianshanagent.plugins.knowledge.documents import STATUS_OK, BuildResult, build_page
from jianshanagent.plugins.knowledge.fetcher import DiskCache, FetchError, WikiFetcher
from jianshanagent.plugins.knowledge.models import (
    KIND_ANNOUNCEMENT,
    KIND_DATA,
    KIND_ENTRY,
    Chunk,
    Document,
)
from jianshanagent.plugins.knowledge.page_dates import (
    PageDating,
    analyse_page,
    infer_from_histories,
    infer_pages,
)
from jianshanagent.plugins.knowledge.settings import Settings
from jianshanagent.plugins.knowledge.sink import ChunkSink
from jianshanagent.plugins.knowledge.sources import PageRef, WikiSource
from jianshanagent.plugins.knowledge.tables import TableStats

logger = get_logger(__name__)

STATUS_FETCH_FAILED = "fetch_failed"
STATUS_DATA_BACKED = "data_backed"  # 页面不渲染，内容由站点插件建的文档代替
STATUS_EXCLUDED = "excluded"
FETCH_LOG_FILE = "fetch_log.jsonl"


@dataclass(slots=True)
class ProcessResult:
    chunks: list[Chunk] = field(default_factory=list)  # 去重后的切片
    records: list[dict[str, Any]] = field(default_factory=list)  # 每篇来源文档一条
    stats: dict[str, Any] = field(default_factory=dict)
    #: 每个渲染过的页面的日期情况（报告和「缺年份」表格用）
    page_dates: list[PageDating] = field(default_factory=list)
    #: 站点插件留给自己那几节报告用的东西：插件的名字 → 它自己定的结构
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Built:
    """建文档阶段的产物。"""

    documents: list[Document] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    page_dates: list[PageDating] = field(default_factory=list)
    #: 推断日期能用的公告：入库的，加上 ``reference_announcements``
    announcements: list[AnnouncementRef] = field(default_factory=list)
    #: 不入库、只给条目推断日期当依据的公告（站点插件给）
    reference_announcements: list[AnnouncementRef] = field(default_factory=list)
    tables: TableStats = field(default_factory=TableStats)
    #: 建文档时顺手数的各种数（并进 ``stats``）
    totals: Counter[str] = field(default_factory=Counter)
    #: 站点插件建文档时留给后面阶段的东西：插件的名字 → 它自己定的结构
    extras: dict[str, Any] = field(default_factory=dict)


def make_record(
    doc_key: str, title: str, status: str, *, ref: PageRef | None = None, **extra: Any
) -> dict[str, Any]:
    """一篇没有入库的来源文档的记录。"""
    record: dict[str, Any] = {"doc_key": doc_key, "title": title, "status": status}
    if ref is not None:
        record |= {"page_id": ref.page_id, "rev_id": ref.rev_id}
    return record | extra


def announcement_ref(doc: Document, *, in_kb: bool) -> AnnouncementRef | None:
    """一篇带日期的公告里，推断条目日期用得上的部分；不是公告、没有日期返回 None。"""
    if doc.kind != KIND_ANNOUNCEMENT or not doc.date:
        return None
    body = "\n".join(b.render() for b in doc.blocks)
    return AnnouncementRef(
        doc_key=doc.doc_key,
        title=doc.title,
        date=doc.date,
        url=doc.url,
        month_days=month_days(f"{doc.title}\n{body}"),
        in_kb=in_kb,
    )


@dataclass(slots=True)
class BuildContext:
    """建文档阶段交给站点插件的东西。"""

    corpus: RawCorpus
    settings: Settings
    built: Built
    #: 只处理这些标题（调试用）；None = 全部
    only_titles: set[str] | None
    #: 站里已有的条目页标题
    known_titles: frozenset[str]

    def absorb(self, result: BuildResult, ref: PageRef) -> None:
        """收下一份原始内容的处理结果：文档进库，没产出文档的记下原因。"""
        built = self.built
        built.tables.add(result.table_stats)
        for key, title, status in result.skipped:
            built.records.append(make_record(key, title, status, ref=ref))
        built.documents.extend(result.documents)

    def fetch_failed(self, ref: PageRef, error: str, *, key: str = "") -> None:
        """记下一份没取下来的原始内容。"""
        self.built.records.append(
            make_record(key or ref.data_id, ref.title, STATUS_FETCH_FAILED, ref=ref, error=error)
        )


def build_documents(
    corpus: RawCorpus, settings: Settings, *, only_titles: set[str] | None = None
) -> Built:
    """原始内容 → 文档，外加每个页面的日期情况和推断日期用的公告清单。"""
    built = Built()
    records = built.records
    removed: Counter[str] = Counter()
    totals = built.totals
    known_titles = frozenset(ref.title for ref in corpus.listing.pages)
    ctx = BuildContext(corpus, settings, built, only_titles, known_titles)

    # 条目
    story_titles = corpus.story_titles
    for fetched in sorted(corpus.rendered, key=lambda f: f.ref.page_id):
        ref = fetched.ref
        if fetched.error:
            ctx.fetch_failed(ref, fetched.error, key=f"page:{ref.page_id}")
            continue
        result = build_page(
            ref, fetched.value, settings, known_titles=known_titles, story_titles=story_titles
        )
        if result.clean_stats is not None:
            stats = result.clean_stats
            removed.update(stats.removed)
            totals["images"] += stats.images
            totals["images_with_real_alt"] += stats.images_with_real_alt
            totals["red_links"] += stats.red_links
            totals["tabbers"] += stats.tabbers
            totals["raw_text_chars"] += stats.raw_text_chars
            totals["clean_text_chars"] += stats.text_chars
        built.page_dates.append(
            analyse_page(
                ref.page_id,
                ref.title,
                settings.wiki.page_url(ref.title),
                result.page_status,
                result.page_text,
                settings.dates,
            )
        )
        ctx.absorb(result, ref)
    for item in corpus.replaced_pages:
        ref = item.ref
        records.append(
            make_record(
                f"page:{ref.page_id}", ref.title, STATUS_DATA_BACKED, ref=ref, replaced_by=item.by
            )
        )
    for ref in corpus.excluded_pages:
        records.append(make_record(f"page:{ref.page_id}", ref.title, STATUS_EXCLUDED, ref=ref))

    # 站点插件的文档：公告、剧情、由数据集生成的……
    for site in settings.sites:
        site.build_documents(ctx)

    built.announcements = [
        found for doc in built.documents if (found := announcement_ref(doc, in_kb=True)) is not None
    ] + built.reference_announcements
    built.stats = {
        "tables": built.tables.as_dict(),
        "removed": dict(removed.most_common()),
        **built.stats,
        **dict(totals),
    }
    return built


def date_pages(built: Built, corpus: RawCorpus, settings: Settings) -> list[PageDating]:
    """第一来源（同名公告）推断日期。返回还要查页面创建时间的页面。"""
    titles = [ref.title for ref in corpus.listing.pages]
    return infer_pages(built.page_dates, built.announcements, titles, settings.dates)


def apply_dates(built: Built) -> None:
    """把推断结果写到文档上。页面自己写了年份的（``date`` 不为空）一定不带推断值。"""
    by_key = {f"page:{page.page_id}": page for page in built.page_dates}
    for doc in built.documents:
        page = by_key.get(doc.doc_key)
        if page is None or doc.date or page.inference is None or not page.inference.date:
            continue
        doc.date_inferred = page.inference.date
        doc.date_source = page.inference.date_source()


def deduplicate(chunks: list[Chunk]) -> tuple[list[Chunk], dict[str, Any]]:
    """正文完全相同的切片只留第一份，其它出处记到留下那份的 ``also_in`` 里。"""
    first: dict[str, Chunk] = {}
    kept: list[Chunk] = []
    removed: Counter[str] = Counter()
    cross_document = 0
    for chunk in chunks:
        original = first.get(chunk.content_hash)
        if original is None:
            first[chunk.content_hash] = chunk
            kept.append(chunk)
            continue
        removed[chunk.kind] += 1
        cross_document += chunk.doc_key != original.doc_key
        original.also_in.append(
            {
                "doc_key": chunk.doc_key,
                "page_id": chunk.page_id,
                "title": chunk.title,
                "section_path": chunk.section_path,
                "url": chunk.url,
            }
        )
    stats = {
        "removed": sum(removed.values()),
        "removed_by_type": dict(removed),
        "removed_cross_document": cross_document,
        "chunks_with_duplicates": sum(1 for c in kept if c.also_in),
    }
    return kept, stats


@dataclass(slots=True)
class ProcessContext:
    """切片、去重以后交给站点插件的东西。"""

    corpus: RawCorpus
    settings: Settings
    built: Built
    #: 去重后的切片（要入库的那些）；插件可以往上面补东西
    chunks: list[Chunk]
    #: 每篇文档去重前的切片：文档标识 → 切片
    per_doc: dict[str, list[Chunk]]
    #: 去重后留下的切片的 ID
    kept_ids: set[str]
    result: ProcessResult


def process(built: Built, corpus: RawCorpus, settings: Settings) -> ProcessResult:
    """文档 → 切片 → 去重 → 交给站点插件补东西（标实体），并汇总统计。

    日期推断要在这之前做完（见 :func:`run_pipeline`）。
    """
    apply_dates(built)
    documents, records, build_stats = built.documents, built.records, built.stats
    chunk_stats = ChunkStats()
    all_chunks: list[Chunk] = []
    per_doc: dict[str, list[Chunk]] = {}
    for doc in documents:
        chunks = chunk_document(doc, settings.chunk, chunk_stats)
        per_doc[doc.doc_key] = chunks
        all_chunks += chunks
    kept, dedupe_stats = deduplicate(all_chunks)
    kept_ids = {c.chunk_id for c in kept}

    result = ProcessResult(chunks=kept, page_dates=built.page_dates)
    ctx = ProcessContext(corpus, settings, built, kept, per_doc, kept_ids, result)
    for site in settings.sites:
        site.annotate(ctx)

    docs_by_type: Counter[str] = Counter()
    chunks_before: Counter[str] = Counter(c.kind for c in all_chunks)
    chunks_after: Counter[str] = Counter(c.kind for c in kept)
    doc_records: list[dict[str, Any]] = []
    for doc in documents:
        chunks = per_doc[doc.doc_key]
        docs_by_type[doc.kind] += 1
        doc_records.append(
            {
                "doc_key": doc.doc_key,
                "title": doc.title,
                "status": STATUS_OK,
                "type": doc.kind,
                "page_id": doc.page_id,
                "rev_id": doc.rev_id,
                "date": doc.date,
                "date_inferred": doc.date_inferred,
                "date_source": doc.date_source,
                "url": doc.url,
                "source": doc.source,
                "chars": doc.text_chars(),
                "chunks": len(chunks),
                "chunks_kept": sum(1 for c in chunks if c.chunk_id in kept_ids),
                # 整篇内容的指纹：修订号没变但渲染结果变了（模板、模块、页面以外的数据改了）
                # 时靠它发现
                "content_hash": sha256_hex("\n".join(c.content_hash for c in chunks)),
                # 文档元数据里约定好的几个标记（见 models.Document.meta），记录里也带上
                **({"title_only": True} if doc.meta.get("title_only") else {}),
                **({"repost": True} if doc.meta.get("repost") else {}),
                **({"nav": True} if doc.meta.get("nav") else {}),
                **({"category": doc.meta["category"]} if doc.kind == KIND_DATA else {}),
            }
        )
    stats: dict[str, Any] = {
        "documents_by_type": dict(docs_by_type),
        "documents_by_source": dict(
            Counter(d.source.split("·")[0] for d in documents if d.kind == KIND_ANNOUNCEMENT)
        ),
        "title_only_posts": sum(1 for d in documents if d.meta.get("title_only")),
        "embed_false_chunks": sum(1 for c in kept if not c.embed),
        "entry_dates": dict(
            Counter(
                str(d.meta.get("date_basis") or "none") for d in documents if d.kind == KIND_ENTRY
            )
        ),
        "entry_dates_inferred": dict(
            Counter(
                str((d.date_source or {}).get("kind"))
                for d in documents
                if d.kind == KIND_ENTRY and d.date_inferred
            )
        ),
        "page_date_categories": dict(Counter(p.category for p in built.page_dates)),
        "announcements_for_dating": {
            "in_kb": sum(1 for a in built.announcements if a.in_kb),
            "reference_only": sum(1 for a in built.announcements if not a.in_kb),
        },
        "directories": {
            "documents": sum(1 for d in documents if d.meta.get("nav")),
            "chunks": sum(1 for c in kept if c.meta.get("nav")),
            "pages": [
                {"title": d.title, **d.meta["nav_links"]} for d in documents if d.meta.get("nav")
            ],
        },
        "chunks_before_dedupe": dict(chunks_before),
        "chunks_after_dedupe": dict(chunks_after),
        "dedupe": dedupe_stats,
        "chunking": chunk_stats.as_dict(),
        "build": build_stats,
        "skipped_by_status": dict(Counter(r["status"] for r in records)),
        "corpus": corpus_summary(corpus, settings),
        "sites": list(settings.sites.ids),
    }
    for site in settings.sites:
        stats |= site.stats(ctx)
    result.records = doc_records + records
    result.stats = stats
    return result


def append_fetch_log(cache_dir: Path, entry: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    with (cache_dir / FETCH_LOG_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_fetch_log(cache_dir: Path) -> list[dict[str, Any]]:
    path = cache_dir / FETCH_LOG_FILE
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


class Held(RuntimeError):
    """结果看着不对（比上一次少了一大截、失败太多），没有写出去。上一次的输出原样留着。"""


#: 看一眼处理结果：返回不该写出去的原因，没问题返回 None
Gate = Callable[[ProcessResult], str | None]


async def run_pipeline(
    settings: Settings,
    sink: ChunkSink,
    *,
    offline: bool = False,
    refresh: bool = False,
    only_titles: set[str] | None = None,
    reuse_listing: bool = False,
    fresh_titles: Collection[str] = (),
    refresh_since: float = 0.0,
    fetcher: WikiFetcher | None = None,
    gate: Gate | None = None,
) -> ProcessResult:
    """跑一遍管线。``only_titles`` 给定时只处理这些页面 / Data 文档（调试用）。

    ``reuse_listing``：沿用上次的页面清单（页面和修订号不重新列），只补清单里缺的数据集。
    后面几个参数是自动更新（sync.py）用的：

    - ``fresh_titles``：这些页面的内容来自的 Data 文档改了，按源码现渲染（见 sources.py 顶部）。
    - ``refresh_since``：``refresh`` 做到一半停了、这次接着做时，上一次是什么时候开始的。
    - ``fetcher``：用调用方给的这一个（请求数的上限和别的步骤合在一起算）。用完不关、
      也不记取数记录，都由调用方做。
    - ``gate``：写输出之前把处理结果交给它看一眼，它说不行就抛 :class:`Held`，什么都不写。
    """
    started = time.time()
    cache = DiskCache(settings.cache_dir)
    if offline:
        fetcher = None
    own_fetcher = fetcher is None and not offline
    if own_fetcher:
        fetcher = WikiFetcher(settings.wiki, settings.fetch)
    requests_before = fetcher.stats.requests if fetcher is not None else 0
    try:
        source = WikiSource(
            fetcher,
            cache,
            settings,
            refresh=refresh,
            reuse_listing=reuse_listing,
            fresh_titles=fresh_titles,
            refresh_since=refresh_since,
        )
        corpus = await collect(source, settings, only_titles=only_titles)
        built = build_documents(corpus, settings, only_titles=only_titles)
        # 日期：同名公告推断不出来的页面，再查它的创建时间（每页两个请求，结果进缓存）
        pending = date_pages(built, corpus, settings)
        refs = {ref.page_id: ref for ref in corpus.listing.pages}
        histories: dict[int, PageHistory] = {}
        for page in pending:
            try:
                history = await source.page_history(refs[page.page_id])
            except (FetchError, KeyError) as exc:
                logger.warning("kb_page_history_failed", title=page.title, error=str(exc))
                continue
            histories[page.page_id] = PageHistory(
                history.get("created_at", ""), history.get("last_edited_at", "")
            )
        infer_from_histories(pending, histories, settings.dates)
    finally:
        if own_fetcher and fetcher is not None:
            await fetcher.aclose()
    fetched_at = time.time()
    fetch_stats = fetcher.stats.as_dict() if fetcher is not None else {"requests": 0}
    # 共用的 fetcher 在这之前可能已经发过请求（自动更新先读了最近更改）：这里只算管线自己发的
    requests = fetch_stats["requests"] - requests_before
    if fetcher is not None and own_fetcher:
        append_fetch_log(
            settings.cache_dir,
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                "seconds": round(fetched_at - started, 1),
                "scope": "指定页面" if only_titles is not None else "全量",
                **fetch_stats,
            },
        )
    logger.info("kb_collected", **corpus_summary(corpus, settings), requests=requests)

    result = process(built, corpus, settings)
    result.stats["run"] = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "fetch_seconds": round(fetched_at - started, 1),
        "offline": offline,
        "fetch": fetch_stats | {"requests": requests},
        "listing_fetched_at": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(corpus.listing.fetched_at)
        ),
        # 重新渲染了几个页面（按原因分）；新的一版没取下来、沿用旧内容的有哪些
        "renders": dict(source.renders),
        "stale": [dataclasses.asdict(item) for item in source.stale],
    }
    if source.stale:
        logger.warning(
            "kb_stale_content_kept",
            count=len(source.stale),
            titles=[item.title for item in source.stale[:10]],
            error=source.stale[0].error[:200],
        )
    if gate is not None:
        reason = gate(result)
        if reason:
            raise Held(reason)
    sink.begin()
    try:
        for record in result.records:
            sink.write_document(record)
        for chunk in result.chunks:
            sink.write_chunk(chunk)
    except BaseException:
        sink.abort()
        raise
    sink.commit()
    result.stats["run"]["process_seconds"] = round(time.time() - fetched_at, 1)
    logger.info("kb_processed", chunks=len(result.chunks), documents=len(result.records))
    return result
