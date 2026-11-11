"""质量报告：把一次运行的统计、问题清单、验收点和抽样写成 Markdown。

核心写和站点无关的几节；站点插件在留好的几个位置（``SLOT_*``）加自己的内容（见 site.py 的
``report``）。节号是按实际写出来的顺序排的，节与节之间的引用用 :meth:`ReportWriter.ref`。
"""

from __future__ import annotations

import json
import random
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from jianshanagent.plugins.knowledge.dates import SOURCE_LABELS
from jianshanagent.plugins.knowledge.models import (
    KIND_ANNOUNCEMENT,
    KIND_DATA,
    KIND_ENTRY,
    KIND_LABELS,
    KIND_STORY,
    Chunk,
)
from jianshanagent.plugins.knowledge.page_dates import (
    CATEGORY_NO_YEAR,
    CATEGORY_PARTIAL,
    CATEGORY_TITLE_YEAR,
    CATEGORY_WIDGET,
    csv_rows,
    write_missing_year_csv,
)
from jianshanagent.plugins.knowledge.pipeline import (
    STATUS_DATA_BACKED,
    STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    ProcessResult,
    read_fetch_log,
)
from jianshanagent.plugins.knowledge.settings import Settings

STATS_FILE = "run_stats.json"
REPORT_FILE = "report.md"
MISSING_YEAR_FILE = "export/活动页缺年份.csv"

#: 留给站点插件的位置
SLOT_COUNTS = "counts"  # 第 1 节里，各类型的数量之后：插件的来源有什么要交代的
SLOT_REPLACED = "replaced"  # 第 2 节里「不渲染、由别的文档代替」那一行：插件说清楚是哪几类
SLOT_PAGES = "pages"  # 第 2 节里，页面去向之后：插件另外取了哪些原始内容
SLOT_PROBLEMS = "problems"  # 问题清单之后：插件自己的节
SLOT_DATES = "dates"  # 日期一节之后：插件自己的节
SLOT_END = "end"  # 最后：插件自己的节

_KINDS = (KIND_ENTRY, KIND_ANNOUNCEMENT, KIND_STORY, KIND_DATA)
#: 「历次取数记录」里最多列最近多少条
_FETCH_LOG_ROWS = 40
#: 重新渲染一个页面的原因（sources.py 的 ``RENDER_*``）→ 报告里的说法
_RENDER_LABELS = {
    "revision": "页面有新修订",
    "touched": "用到的模板或模块改了",
    "data": "内容来自的数据改了（按源码现渲染）",
    "refresh": "全部重取",
}
#: 核心自己的「没有入库的原因」；站点插件定的由插件给说法（``status_labels``）
_CORE_STATUSES = ("empty", "nav", "broken")
_REMOVED_LABELS = {
    "<img>": "图片",
    "<script>": "脚本",
    "<style>": "样式",
    "display:none": "内联 display:none 的元素",
    "[hidden]": "带 hidden 属性的元素",
}


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    return values[min(len(values) - 1, int(len(values) * q))]


def _length_row(label: str, lengths: list[int]) -> str:
    lengths = sorted(lengths)
    if not lengths:
        return f"| {label} | 0 | - | - | - | - | - | - | - | - |"
    cells = [
        len(lengths),
        lengths[0],
        _percentile(lengths, 0.10),
        _percentile(lengths, 0.25),
        _percentile(lengths, 0.50),
        round(statistics.fmean(lengths)),
        _percentile(lengths, 0.75),
        _percentile(lengths, 0.90),
        lengths[-1],
    ]
    return f"| {label} | " + " | ".join(str(c) for c in cells) + " |"


class ReportWriter:
    """一行一行攒报告。节号按写出来的顺序排。"""

    _REF = re.compile(r"\x00ref:([^\x00]*)\x00")

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._number = 0
        self._sections: dict[str, int] = {}

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def section(self, title: str, key: str = "") -> None:
        """开一节。``key`` 是给别处引用它用的名字。"""
        self._number += 1
        if key:
            self._sections[key] = self._number
        self.add(f"## {self._number}. {title}")
        self.add("")

    def ref(self, key: str) -> str:
        """「第 N 节」。那一节可以还没写到；最后也没有这一节的话写成「后文」。"""
        return f"\x00ref:{key}\x00"

    def render(self) -> str:
        def number(match: re.Match[str]) -> str:
            found = self._sections.get(match.group(1))
            return f"第 {found} 节" if found else "后文"

        return self._REF.sub(number, "\n".join(self.lines))


def status_labels(out: ReportWriter, settings: Settings) -> dict[str, str]:
    """「没有入库的原因」→ 报告里的说法：核心的，加上各个站点插件的。"""
    labels = {
        "empty": "清洗后为空",
        "nav": f"纯导航页（只有编号链接的翻页导航；章节目录页已入库，见{out.ref('directories')}）",
        "broken": "页面只渲染出一句报错（它引用的数据不存在）",
        STATUS_FETCH_FAILED: "取数失败",
        STATUS_DATA_BACKED: "不渲染，内容由别的文档代替",
        STATUS_EXCLUDED: "配置里排除",
    }
    for site in settings.sites:
        labels |= site.status_labels()
    return labels


def fenced(text: str) -> str:
    return f"````text\n{text}\n````"


def entity_line(chunk: Chunk) -> str:
    """一个切片的实体：确定的在前，候选在后（写明靠哪个昵称）。"""
    certain = [f"{e['name']}（{e['type']}）" for e in chunk.entities if e["certain"]]
    candidates = [
        f"{e['name']}（{e['type']}，靠「{e['via']}」）" for e in chunk.entities if not e["certain"]
    ]
    parts = []
    if certain:
        parts.append("实体：" + "、".join(certain))
    if candidates:
        parts.append("候选实体：" + "、".join(candidates))
    return "；".join(parts)


def chunk_block(chunk: Chunk) -> str:
    meta = [
        f"类型：{KIND_LABELS.get(chunk.kind, chunk.kind)}",
        f"正文 {len(chunk.body)} 字",
        f"ID：`{chunk.chunk_id}`",
        f"页面 ID {chunk.page_id} / 修订号 {chunk.rev_id}",
    ]
    if chunk.date:
        meta.append(f"日期：{chunk.date}")
    if chunk.date_inferred:
        meta.append(f"推断年月：{chunk.date_inferred}")
    if not chunk.embed:
        meta.append("embed: false")
    if chunk.entities:
        meta.append(entity_line(chunk))
    if chunk.also_in:
        meta.append(f"另有 {len(chunk.also_in)} 处相同内容")
    return f"{'；'.join(meta)}\n\n链接：<{chunk.url}>\n\n{fenced(chunk.text)}"


def _titles(records: list[dict[str, Any]], status: str, limit: int = 400) -> str:
    titles = [str(r["title"]) for r in records if r["status"] == status]
    if not titles:
        return "（无）"
    shown = "、".join(f"`{t}`" for t in titles[:limit])
    return shown + (f" …（共 {len(titles)} 个）" if len(titles) > limit else "")


def _date_section(out: ReportWriter, result: ProcessResult) -> None:
    add = out.add
    pages = result.page_dates
    stats = result.stats
    dates = stats.get("entry_dates") or {}
    out.section("条目的日期：页面自己写明的、推断的、未能推断的", "dates")
    add(
        "先认页面自己写明的日期，认不出来、而全页（含标题）又没有任何年份时才推断；"
        "页面写了年份就不会有 `date_inferred`。"
        "所以 wiki 上补了年份以后，重跑即自动改用页面自己的日期。"
    )
    add("")
    add(
        f"- 页面自己写明：标题 {dates.get('title', 0)} 个；"
        f"正文里紧跟「活动时间」等字样的日期 {dates.get('content', 0)} 个；"
        f"正文开头的日期范围 {dates.get('content_lead', 0)} 个"
    )
    add(f"- 页面罗列了相隔三个月以上的多个活动日期、拿不准而留空：{dates.get('ambiguous', 0)} 个")
    add("")
    own = sorted(
        (p for p in pages if p.status == "ok" and p.date and p.basis.startswith("content")),
        key=lambda p: p.title,
    )
    if own:
        add("从正文认出日期的条目：")
        add("")
        add("| 条目 | 日期 | 依据 |")
        add("| --- | --- | --- |")
        labels = {"content": "紧跟时间字样", "content_lead": "正文开头的日期范围"}
        for page in own:
            add(f"| {page.title} | {page.date} | {labels.get(page.basis, page.basis)} |")
        add("")
    ambiguous = sorted(p.title for p in pages if p.status == "ok" and p.basis == "ambiguous")
    if ambiguous:
        add("拿不准而留空的：" + "、".join(f"`{t}`" for t in ambiguous))
        add("")

    inferred = sorted(
        (p for p in pages if p.status == "ok" and p.inference and p.inference.date),
        key=lambda p: p.title,
    )
    add(f"**推断出年月的条目（{len(inferred)} 个）**")
    add("")
    if inferred:
        add("| 条目 | 推断年月 | 来源 | 依据 |")
        add("| --- | --- | --- | --- |")
        for page in inferred:
            inference = page.inference
            assert inference is not None
            source = SOURCE_LABELS.get(inference.source, inference.source)
            add(f"| {page.title} | {inference.date} | {source} | {inference.basis} |")
        add("")
    failed = sorted(
        (p for p in pages if p.needs_inference and not (p.inference and p.inference.date)),
        key=lambda p: p.title,
    )
    widgets = sorted(p.title for p in pages if p.category == CATEGORY_WIDGET)
    add(f"**未能推断（{len(failed) + len(widgets)} 个）**")
    add("")
    for page in failed:
        reason = page.inference.reason if page.inference else "没有做推断"
        add(f"- `{page.title}`（页面状态 {page.status}）：{reason}")
    for title in widgets:
        add(f"- `{title}`：内容天天在变的页面，按配置不推断")
    if not failed and not widgets:
        add("（无）")
    add("")
    partial = sorted((p for p in pages if p.category == CATEGORY_PARTIAL), key=lambda p: p.title)
    if partial:
        add(
            f"**页面别处写了年份、但有些月日没带的（{len(partial)} 个，不推断）**："
            + "、".join(
                f"`{p.title}`（缺 {p.bare_count} 处"
                + (f"，页面日期已认出 {p.date}" if p.date else "，页面日期认不出")
                + "）"
                for p in partial
            )
        )
        add("")
    title_year = sum(1 for p in pages if p.category == CATEGORY_TITLE_YEAR)
    excluded: Counter[str] = Counter()
    for page in pages:
        excluded.update(page.excluded)
    add(
        f"「活动页缺年份」表格（`export/活动页缺年份.csv`）共 {len(csv_rows(pages))} 行："
        f"全页没有年份的 {sum(1 for p in pages if p.category == CATEGORY_NO_YEAR)} 页，"
        "加上页面别处有年份、但页面自己的活动日期认不出来的 "
        f"{sum(1 for p in pages if p.category == CATEGORY_PARTIAL and p.date is None)} 页。"
        f"没有列入的：标题带年份的 {title_year} 页，按配置不推断的 {len(widgets)} 页；"
        "按「生日 / 每年」等字样排除的日期 "
        + ("、".join(f"{k} {v} 处" for k, v in excluded.items()) if excluded else "0 处")
        + "。"
    )
    add("")


def _directory_section(out: ReportWriter, result: ProcessResult) -> None:
    add = out.add
    stats = result.stats.get("directories") or {}
    out.section("章节目录页", "directories")
    add(
        f"原先判为纯导航的页面里，{stats.get('documents', 0)} 个是章节目录页，已入库"
        f"（{stats.get('chunks', 0)} 片，元数据带 `nav: true`，`embed` 为 `true`）。"
        "判定只看页面内容：站内链接里「文字是名字、指向已有页面」的不少于 3 个且占一半以上。"
        f"只有编号链接的翻页导航达不到，仍然不入库（见{out.ref('problems')}）。"
    )
    add("")
    add("| 页面 | 站内链接 | 文字是名字的 | 指向本页子页面 | 指向剧情页面 | 指向别的条目 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for page in stats.get("pages") or []:
        add(
            f"| {page['title']} | {page['total']} | {page['named']} | {page['subpages']} "
            f"| {page['story_pages']} | {page['other_pages']} |"
        )
    add("")
    chunks = [c for c in result.chunks if c.meta.get("nav")]
    for chunk in chunks[:2]:
        add(f"**{chunk.title}** 处理后的全文：")
        add("")
        add(chunk_block(chunk))
        add("")


def build_report(result: ProcessResult, settings: Settings) -> str:
    stats = result.stats
    chunks = result.chunks
    records = result.records
    cfg = settings.chunk
    out = ReportWriter()
    add = out.add
    labels_by_status = status_labels(out, settings)

    def slot(name: str) -> bool:
        """让站点插件在这个位置加内容；返回有没有插件加了东西。"""
        before = len(out.lines)
        for site in settings.sites:
            site.report(name, out, result, settings)
        return len(out.lines) > before

    run = stats.get("run") or {}
    add("# wiki 知识库预处理报告")
    add("")
    add(
        f"- 运行时间：{run.get('started_at', '')}；页面清单取于 {run.get('listing_fetched_at', '')}"
        f"{'（本次为离线运行，原始内容全部来自缓存）' if run.get('offline') else ''}"
    )
    add(
        f"- 本次耗时：取数 {run.get('fetch_seconds', 0)} 秒，"
        f"清洗与切片 {run.get('process_seconds', 0)} 秒；"
        f"本次对 wiki 发出 {(run.get('fetch') or {}).get('requests', 0)} 个请求"
    )
    renders = run.get("renders") or {}
    if renders:
        add(
            "- 本次重新渲染的页面："
            + "，".join(f"{_RENDER_LABELS.get(k, k)} {v} 个" for k, v in renders.items())
        )
    stale = run.get("stale") or []
    if stale:
        names = "、".join(f"`{item['title']}`" for item in stale[:20])
        add(
            f"- **有 {len(stale)} 份内容这次没取下来，沿用的是缓存里的旧内容**"
            f"（下次运行会再取）：{names}{' 等' if len(stale) > 20 else ''}。"
            f"第一条错误：{stale[0]['error'][:200]}"
        )
    add(
        f"- 切片参数：目标 {cfg.target_chars} 字，上限 {cfg.max_chars} 字，"
        f"并片阈值 {cfg.min_chars} 字（按字符计，不含首行标题路径）"
    )
    add("")

    # ── 1. 数量 ──
    out.section("各类型的文档数和切片数")
    add("| 类型 | 文档数 | 切片数（去重前） | 切片数（去重后） | 去重后正文总字数 |")
    add("| --- | --- | --- | --- | --- |")
    docs = stats["documents_by_type"]
    before = stats["chunks_before_dedupe"]
    after = stats["chunks_after_dedupe"]
    for kind in _KINDS:
        chars = sum(len(c.body) for c in chunks if c.kind == kind)
        add(
            f"| {KIND_LABELS[kind]} | {docs.get(kind, 0)} | {before.get(kind, 0)} "
            f"| {after.get(kind, 0)} | {chars} |"
        )
    add(
        f"| 合计 | {sum(docs.values())} | {sum(before.values())} | {sum(after.values())} "
        f"| {sum(len(c.body) for c in chunks)} |"
    )
    add("")
    sources = Counter(
        (c.source.split("·")[0] if c.kind != KIND_ENTRY else c.source) for c in chunks
    )
    add(
        "按来源分的切片数：" + "，".join(f"{name} {count}" for name, count in sources.most_common())
    )
    add("")
    by_source = stats.get("documents_by_source") or {}
    if by_source:
        add("公告按来源分的文档数：" + "，".join(f"{k} {v}" for k, v in by_source.items()))
        add("")
    slot(SLOT_COUNTS)
    title_only = stats.get("title_only_posts", 0)
    add(
        f"`embed` 字段：所有切片都带。只有标题、没有正文的公告为 `false`"
        f"（本次 {title_only} 篇、{stats.get('embed_false_chunks', 0)} 片），其余为 `true`。"
        + (
            "这些各留了一片只含标题、日期、来源、链接的切片，元数据里另带 `title_only: true`。"
            if title_only
            else ""
        )
    )
    add("")

    # ── 2. 页面去向 ──
    corpus = stats["corpus"]
    by_status = Counter(r["status"] for r in records)
    entry_ok = docs.get(KIND_ENTRY, 0)
    out.section("条目页面的去向")
    add(f"清单里共有 {corpus['pages_listed']} 个非重定向页面：")
    add("")
    add(f"- 渲染后入库：{entry_ok} 个")
    if not slot(SLOT_REPLACED) and corpus.get("replaced_pages"):
        add(f"- 不渲染、内容由别的文档代替（不重复入库）：{corpus['replaced_pages']} 个")
    page_records = [r for r in records if str(r["doc_key"]).startswith("page:")]
    for status in ("empty", "nav", "broken", STATUS_FETCH_FAILED, STATUS_EXCLUDED):
        count = sum(1 for r in page_records if r["status"] == status)
        add(f"- {labels_by_status[status]}：{count} 个")
    add(
        f"- 上面「渲染后入库」里有 {(stats.get('directories') or {}).get('documents', 0)} 个"
        "是章节目录页（改写成带上下文的目录文字，元数据带 `nav: true`）"
    )
    add("")
    slot(SLOT_PAGES)
    dates = stats.get("entry_dates") or {}
    inferred = stats.get("entry_dates_inferred") or {}
    add(
        f"条目的日期（详见{out.ref('dates')}）：页面自己写明的 "
        f"{dates.get('title', 0) + dates.get('content', 0) + dates.get('content_lead', 0)} 个"
        f"（标题 {dates.get('title', 0)}，紧跟「活动时间」等字样的日期 {dates.get('content', 0)}，"
        f"正文开头的日期范围 {dates.get('content_lead', 0)}）；"
        f"推断出年月的 {sum(inferred.values())} 个；"
        f"其余 {dates.get('none', 0) + dates.get('ambiguous', 0) - sum(inferred.values())} 个留空"
        f"（其中 {dates.get('ambiguous', 0)} 个是页面罗列了相隔很远的多个活动日期，拿不准）。"
        "公告都带发帖日期；剧情不带日期。"
    )
    add("")

    # ── 3. 长度分布 ──
    out.section("切片长度分布（正文字符数）")
    add("| 类型 | 切片数 | 最小 | 10% | 25% | 中位数 | 平均 | 75% | 90% | 最大 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for kind in _KINDS:
        add(_length_row(KIND_LABELS[kind], [len(c.body) for c in chunks if c.kind == kind]))
    add(_length_row("全部", [len(c.body) for c in chunks]))
    add("")
    edges = [0, 50, cfg.min_chars, 400, cfg.target_chars, cfg.max_chars]
    labels = [f"{lo}–{hi - 1}" for lo, hi in zip(edges, edges[1:], strict=False)]
    labels[-1] = f"{cfg.target_chars}–{cfg.max_chars}"
    labels.append(f">{cfg.max_chars}（超上限）")
    add("| 区间（字） | " + " | ".join(labels) + " |")
    add("| --- |" + " --- |" * len(labels))
    for bucket_kind in (*_KINDS, ""):
        lengths = [len(c.body) for c in chunks if not bucket_kind or c.kind == bucket_kind]
        buckets = [
            sum(1 for n in lengths if lo <= n < hi)
            for lo, hi in zip(edges, edges[1:], strict=False)
        ]
        buckets.append(sum(1 for n in lengths if n > cfg.max_chars))
        buckets[-2] += sum(1 for n in lengths if n == cfg.max_chars)
        add(
            f"| {KIND_LABELS.get(bucket_kind, '全部')} | "
            + " | ".join(str(b) for b in buckets)
            + " |"
        )
    add("")
    oversize = sorted(
        (c for c in chunks if len(c.body) > cfg.max_chars), key=lambda c: -len(c.body)
    )
    if oversize:
        add(
            f"超过上限的 {len(oversize)} 片"
            "（都是单行表格、单个列表条目或没有标点的长句，没有硬切）："
        )
        add("")
        for chunk in oversize[:15]:
            add(f"- {len(chunk.body)} 字：{chunk.header}")
        add("")

    # ── 4. 去重 ──
    dedupe = stats["dedupe"]
    out.section("去重")
    add(
        f"正文完全相同的切片只留一份：去掉 {dedupe['removed']} 片"
        f"（其中 {dedupe['removed_cross_document']} 片与留下的那份不在同一篇文档里），"
        f"涉及 {dedupe['chunks_with_duplicates']} 片留下的切片，其它出处记在它的 `also_in` 里。"
    )
    by_type = dedupe.get("removed_by_type") or {}
    if by_type:
        add("")
        add("按类型：" + "，".join(f"{KIND_LABELS.get(k, k)} {v}" for k, v in by_type.items()))
    repeated = sorted((c for c in chunks if c.also_in), key=lambda c: -len(c.also_in))[:8]
    if repeated:
        add("")
        add("重复次数最多的几片：")
        add("")
        for chunk in repeated:
            preview = chunk.body.replace("\n", " ")[:60]
            add(f"- 另有 {len(chunk.also_in)} 处相同：{chunk.header} ——「{preview}…」")
    add("")

    # ── 5. 表格 ──
    tables = stats["build"]["tables"]
    chunking = stats["chunking"]
    out.section("表格处理")
    add(
        f"- 遇到的表格：{tables['total']} 张；输出为 Markdown 表格 "
        f"{tables['markdown_tables']} 张，共 {tables['rows']} 行正文"
    )
    add(f"- 含合并单元格（rowspan / colspan）、展开成规整网格的：{tables['merged_cells']} 张")
    add(f"- 多行表头、按列合并成一行的：{tables['multi_header']} 张")
    add(f"- 含嵌套表格、外层按排版表格逐格展开的：{tables['nested']} 张")
    add(f"- 没有表头行、把第一行当表头的（三列及以上）：{tables['headerless']} 张")
    add(f"- 没有表头行的两列表格，转成「名称：值」列表的：{tables['key_value']} 张")
    add(f"- 没有表头行的一列表格，转成普通文字的：{tables['single_column']} 张")
    add(f"- 清洗后没有内容、丢弃的：{tables['empty']} 张")
    add(f"- 因为整行只有占位值而去掉的表格行：{tables['placeholder_rows']} 行")
    add(
        f"- 切片时因超过上限被按行拆开的表格：{chunking['split_tables']} 张，"
        f"拆成 {chunking['table_pieces']} 片，"
        "每片都重复表题和表头"
    )
    add(
        f"- 其它拆分：按条目拆开的列表 {chunking['split_lists']} 个，"
        f"按句子拆开的段落 {chunking['split_paragraphs']} 个，"
        f"按字数硬切 {chunking['hard_splits']} 次，"
        f"原样保留的超长单行 / 单条 {chunking['oversize_units']} 个；"
        f"并进相邻切片的小节 {chunking['merged_sections']} 个"
    )
    add("")

    # ── 6. 清洗 ──
    build = stats["build"]
    raw, cleaned = build.get("raw_text_chars", 0), build.get("clean_text_chars", 0)
    out.section("清洗（只统计渲染的条目页面）")
    if raw:
        add(f"- 清洗前文字 {raw} 字，清洗后 {cleaned} 字，去掉 {100 - cleaned * 100 // raw}%")
    add(
        f"- 图片 {build.get('images', 0)} 张全部移除（其中 alt 是周围文字里没有的名字、留成文字的 "
        f"{build.get('images_with_real_alt', 0)} 张）；"
        f"红链 {build.get('red_links', 0)} 个退化为文字或移除；"
        f"标签页 {build.get('tabbers', 0)} 组改写成小标题"
    )
    add("")
    add("| 移除的元素 | 个数 |")
    add("| --- | --- |")
    for reason, count in list((build.get("removed") or {}).items())[:40]:
        add(f"| {_REMOVED_LABELS.get(reason, reason)} | {count} |")
    add("")

    # ── 7. 问题清单 ──
    out.section("清洗后为空、纯导航、取数失败的清单", "problems")
    core_statuses = (STATUS_FETCH_FAILED, *_CORE_STATUSES, STATUS_DATA_BACKED, STATUS_EXCLUDED)
    site_statuses = [status for status in labels_by_status if status not in core_statuses]
    for status in (STATUS_FETCH_FAILED, *_CORE_STATUSES, *site_statuses, STATUS_EXCLUDED):
        count = by_status.get(status, 0)
        add(f"**{labels_by_status[status]}（{count}）**：{_titles(records, status, 120)}")
        add("")
    failed = [r for r in records if r["status"] == STATUS_FETCH_FAILED]
    for record in failed[:30]:
        add(f"- `{record['title']}`：{record.get('error', '')}")
    if failed:
        add("")

    slot(SLOT_PROBLEMS)
    _date_section(out, result)
    slot(SLOT_DATES)

    # ── 验收点 ──
    report = settings.report
    if report.acceptance_page:
        needles = report.acceptance_must_contain
        out.section("验收点")
        add(
            f"要求：页面「{report.acceptance_page}」里同时含有 "
            f"{'、'.join(f'「{n}」' for n in needles)} 的切片，必须带着表头和标题路径。"
        )
        add("")
        hits = [
            c
            for c in chunks
            if c.title == report.acceptance_page and all(n in c.body for n in needles)
        ]
        if not hits:
            add("**没有找到符合条件的切片。**")
            add("")
        for chunk in hits:
            row = next(
                (line for line in chunk.body.split("\n") if all(n in line for n in needles)), ""
            )
            lines = chunk.body.split("\n")
            has_header = any(
                line.startswith("|")
                and set(line) <= set("|- ")
                and i > 0
                and lines[i - 1].startswith("|")
                for i, line in enumerate(lines)
            )
            add(f"- 所在行：`{row}`")
            add(f"- 标题路径（切片首行）：`{chunk.header}`")
            add(f"- 切片里有表头行和分隔线：{'是' if has_header else '否'}")
            add(f"- `date`：`{chunk.date}`；`date_inferred`：`{chunk.date_inferred}`")
            add(f"- `date_source`：`{json.dumps(chunk.date_source, ensure_ascii=False)}`")
            add(f"- `entities`：`{json.dumps(chunk.entities, ensure_ascii=False)}`")
            add("")
            add(chunk_block(chunk))
            add("")

    # ── 抽样 ──
    rng = random.Random(report.sample_seed)
    sample = rng.sample(chunks, min(report.sample_size, len(chunks)))
    out.section(f"随机抽样 {len(sample)} 个切片的全文（随机种子 {report.sample_seed}）")
    for index, chunk in enumerate(sample, 1):
        add(f"### 抽样 {index}")
        add("")
        add(chunk_block(chunk))
        add("")

    # ── 取数记录 ──
    log = stats.get("fetch_log") or []
    if log:
        out.section("历次取数记录")
        add(
            "| 时间 | 范围 | 耗时（秒） | 请求数 "
            "| 其中：清单 / 源码 / 渲染 / Data / 页面历史 / 最近更改 | 重试 | 缓存命中 | 备注 |"
        )
        add("| --- | --- | --- | --- | --- | --- | --- | --- |")
        # 自动更新每做一轮事记一条，日子久了很长：只列最近的，更早的合成一行
        earlier, recent = log[:-_FETCH_LOG_ROWS], log[-_FETCH_LOG_ROWS:]
        if earlier:
            total = sum(int(e.get("requests", 0)) for e in earlier)
            span = f"{earlier[0].get('at', '')} 至 {earlier[-1].get('at', '')}"
            note = "明细在缓存目录的 fetch_log.jsonl"
            add(f"| {span} | 更早的 {len(earlier)} 条 |  | {total} |  |  |  | {note} |")
        for entry in recent:
            kinds = dict(entry.get("by_kind") or {})
            # 探查脚本把「查页面创建时间」记成 created，和管线里的 history 是同一类请求；
            # 「读最近更改」记成 rc，和自动更新里的 changes 是同一类
            kinds["history"] = kinds.get("history", 0) + kinds.pop("created", 0)
            kinds["changes"] = kinds.get("changes", 0) + kinds.pop("rc", 0)
            parts = " / ".join(
                str(kinds.get(k, 0))
                for k in ("list", "wikitext", "parse", "data", "history", "changes")
            )
            add(
                f"| {entry.get('at', '')} | {entry.get('scope', '')} | {entry.get('seconds', '')} "
                f"| {entry.get('requests', 0)} | {parts} | {entry.get('retries', 0)} "
                f"| {entry.get('cache_hits', 0)} | {entry.get('note', '')} |"
            )
        add("")
        add(f"合计请求数：{sum(int(e.get('requests', 0)) for e in log)}")
        add("")

    _directory_section(out, result)
    slot(SLOT_END)
    return out.render()


def write_report(path: Path, result: ProcessResult, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(result, settings), encoding="utf-8")


def write_outputs(output_dir: Path, result: ProcessResult, settings: Settings) -> int:
    """一次运行除切片以外的产物：统计、报告、「活动页缺年份」表格。返回表格的行数。"""
    result.stats["fetch_log"] = read_fetch_log(settings.cache_dir)
    (output_dir / STATS_FILE).write_text(
        json.dumps(result.stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(output_dir / REPORT_FILE, result, settings)
    return write_missing_year_csv(output_dir / MISSING_YEAR_FILE, result.page_dates)
