"""把渲染后的条目页面变成待切片的文档，外加站点插件建文档时用得上的几个小工具。

站点插件自己的原始内容（公告、剧情、数据集……）由插件变成文档（见 site.py 的 ``build_documents``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jianshanagent.plugins.knowledge.cleaner import CleanStats, clean, parse_html
from jianshanagent.plugins.knowledge.dates import page_date
from jianshanagent.plugins.knowledge.directory import analyse_links, directory_blocks
from jianshanagent.plugins.knowledge.markdown import html_to_blocks, normalize_text
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_PARA,
    KIND_ENTRY,
    Block,
    Document,
)
from jianshanagent.plugins.knowledge.settings import ChunkSettings, CleanSettings, Settings
from jianshanagent.plugins.knowledge.sources import PageRef
from jianshanagent.plugins.knowledge.tables import TableStats

STATUS_OK = "ok"
STATUS_EMPTY = "empty"  # 清洗后没有内容
STATUS_NAV = "nav"  # 纯导航页（只有编号链接的翻页导航；章节目录页入库，meta 带 nav）
STATUS_BROKEN = "broken"  # 页面渲染结果只有一句报错

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


@dataclass(slots=True)
class BuildResult:
    """一份原始内容的处理结果：产出的文档，以及没产出文档时的原因。"""

    documents: list[Document] = field(default_factory=list)
    #: 没有产出文档的条目：（标识，标题，状态）
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    clean_stats: CleanStats | None = None
    table_stats: TableStats = field(default_factory=TableStats)
    #: 条目页面清洗后的正文和去向（不入库的页面也给，查「缺年份」时要用）
    page_text: str = ""
    page_status: str = ""


def plain_blocks(text: str, *, tight: bool = False) -> list[Block]:
    """纯文本 → 段落块：空行分段，段内换行保留。"""
    text = normalize_text(_TAG_RE.sub("", _BR_RE.sub("\n", text or "")))
    blocks: list[Block] = []
    for para in re.split(r"\n\s*\n", text):
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in para.split("\n")]
        body = "\n".join(line for line in lines if line)
        if body:
            blocks.append(Block(BLOCK_PARA, text=body, tight=tight))
    return blocks


def html_blocks(
    html: str, clean_cfg: CleanSettings, chunk_cfg: ChunkSettings, table_stats: TableStats
) -> list[Block]:
    """不是 wiki 页面的 HTML（帖子自带的正文）→ 块。只做通用清洗，不套 wiki 页面的组件规则。"""
    root = parse_html(html)
    clean(root, clean_cfg, wiki=False)
    blocks, stats = html_to_blocks(root, label_max_chars=chunk_cfg.label_max_chars)
    table_stats.add(stats)
    return blocks


def has_text(blocks: list[Block]) -> bool:
    return any(b.kind != BLOCK_HEADING and b.size() for b in blocks)


# ── 条目页面 ──────────────────────────────────────────────────────────


def build_page(
    ref: PageRef,
    parsed: dict[str, Any],
    settings: Settings,
    *,
    known_titles: frozenset[str] | set[str] | None = None,
    story_titles: frozenset[str] | set[str] = frozenset(),
) -> BuildResult:
    """渲染后的页面 → 文档。

    ``known_titles`` 是站里已有的页面标题、``story_titles`` 是其中由剧情文档代替的页面：
    只在判断「章节目录页」时用来核对链接指向哪里；不给就不核对。
    """
    result = BuildResult()
    root = parse_html(str(parsed.get("text") or ""))
    stats = clean(root, settings.clean, wiki=True)
    result.clean_stats = stats
    blocks, table_stats = html_to_blocks(
        root,
        label_max_chars=settings.chunk.label_max_chars,
        table_placeholders=settings.clean.table_placeholders,
    )
    result.table_stats.add(table_stats)

    doc = Document(
        doc_key=f"page:{ref.page_id}",
        kind=KIND_ENTRY,
        title=ref.title,
        url=settings.wiki.page_url(ref.title),
        source="wiki 条目",
        page_id=ref.page_id,
        rev_id=ref.rev_id,
        blocks=blocks,
    )
    src = settings.sources
    key = doc.doc_key
    body = "\n".join(b.render() for b in blocks)
    result.page_text = body
    # 纯导航页：站内链接的文字占了绝大部分，链接以外几乎没有内容（剧情目录、活动目录之类）
    plain = stats.nav_text_chars - stats.nav_link_chars
    link_heavy = (
        stats.nav_text_chars and stats.nav_link_chars / stats.nav_text_chars >= src.nav_link_ratio
    )
    nav_meta: dict[str, Any] = {}
    if link_heavy and plain < src.nav_max_plain_chars and stats.nav_link_chars >= src.min_chars:
        # 章节目录页（链接文字是章节名、活动名）入库并改写成带上下文的文字；
        # 只有编号的翻页导航（花合战漫画月份页）仍然不入库。见 directory.py
        info = analyse_links(
            root,
            ref.title,
            known_titles=known_titles,
            story_titles=story_titles,
            link_prefix=settings.wiki.link_prefix,
        )
        if info.is_directory(src.directory_min_links, src.directory_min_share):
            doc.blocks = directory_blocks(blocks, info.links or [], ref.title)
            body = "\n".join(b.render() for b in doc.blocks)
            result.page_text = body
            nav_meta = {
                "nav": True,
                "nav_links": {
                    "total": info.internal,
                    "named": info.named,
                    "subpages": info.to_subpages,
                    "story_pages": info.to_story_pages,
                    "other_pages": info.to_other_pages,
                },
            }
        else:
            result.page_status = STATUS_NAV
    elif doc.text_chars() < src.min_chars:
        result.page_status = STATUS_EMPTY
    elif len(body) < src.broken_max_chars and any(m in body for m in src.broken_markers):
        # 页面只渲染出一句模块报错（它引用的数据不存在）
        result.page_status = STATUS_BROKEN
    if result.page_status:
        result.skipped.append((key, ref.title, result.page_status))
        return result
    result.page_status = STATUS_OK
    doc.date, basis = page_date(ref.title, body, settings.dates)
    categories = [
        str(c.get("category") or "").replace("_", " ")
        for c in parsed.get("categories") or []
        if not c.get("hidden")
    ]
    doc.meta = {
        k: v for k, v in {"categories": categories, "date_basis": basis, **nav_meta}.items() if v
    }
    result.documents.append(doc)
    return result
