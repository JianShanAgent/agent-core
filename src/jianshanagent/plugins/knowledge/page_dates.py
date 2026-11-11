"""逐页整理日期情况：页面自己写没写年份、哪些日期缺年份、推断结果；并导出「活动页缺年份」表格。

每个渲染过的主命名空间页面（包括纯导航、清洗后为空这些不入库的）都过一遍，分成几类：

- ``none``：正文里没有缺年份的月日；
- ``title_year``：有缺年份的月日，但标题里带年份（``花合战/202108``），不算缺；
- ``widget``：配置里排除的前缀（首页组件），日期每天在变，不算缺、也不推断；
- ``no_year``：全页（含标题）没有任何年份 —— 要推断，也要列进表格；
- ``partial``：页面别处写了年份，但这些月日没带。页面自己的日期认得出来的不用管；
  认不出来的列进表格，备注里说明，不推断。

生日这类本来就不该带年份的日期在 :func:`dates.find_dates` 里就排除了，这里只计数。
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jianshanagent.plugins.knowledge.dates import (
    HISTORICAL,
    SOURCE_ANNOUNCEMENT,
    AnnouncementRef,
    DateMention,
    Inference,
    PageHistory,
    find_dates,
    has_any_year,
    infer_from_announcements,
    infer_from_created,
    mention_context,
    page_date,
)
from jianshanagent.plugins.knowledge.settings import DateSettings

CATEGORY_NONE = "none"
CATEGORY_TITLE_YEAR = "title_year"
CATEGORY_WIDGET = "widget"
CATEGORY_NO_YEAR = "no_year"
CATEGORY_PARTIAL = "partial"

CSV_COLUMNS = (
    "页面标题",
    "页面链接",
    "缺年份的日期原文（前几处）",
    "缺年份的日期共几处",
    "推断的年月",
    "推断依据",
    "备注",
)
_CONTEXT_LIMIT = 4


@dataclass(slots=True)
class PageDating:
    page_id: int
    title: str
    url: str
    status: str  # 页面的去向：ok / nav / empty / broken
    category: str
    #: 页面自己写明的日期和依据（见 dates.page_date）
    date: str | None = None
    basis: str = ""
    bare_count: int = 0
    contexts: list[str] = field(default_factory=list)
    #: 页面里写明年份的日期有几处、都是哪些年
    explicit_count: int = 0
    explicit_years: list[int] = field(default_factory=list)
    #: 排除掉的日期：原因 → 处数（生日、每年……）
    excluded: dict[str, int] = field(default_factory=dict)
    bare: list[DateMention] = field(default_factory=list)
    inference: Inference | None = None

    @property
    def needs_inference(self) -> bool:
        return self.category == CATEGORY_NO_YEAR and self.date is None

    @property
    def listed(self) -> bool:
        """要不要列进「缺年份」表格。"""
        return self.category == CATEGORY_NO_YEAR or (
            self.category == CATEGORY_PARTIAL and self.date is None
        )


def analyse_page(
    page_id: int, title: str, url: str, status: str, text: str, cfg: DateSettings
) -> PageDating:
    mentions = find_dates(text, cfg)
    bare = [m for m in mentions if m.bare]
    date, basis = page_date(title, text, cfg, mentions)
    explicit = [m for m in mentions if m.year is not None and not m.excluded]
    if not bare:
        category = CATEGORY_NONE
    elif basis == "title" or has_any_year(title, "", cfg):
        category = CATEGORY_TITLE_YEAR
    elif any(title.startswith(prefix) for prefix in cfg.exclude_prefixes):
        category = CATEGORY_WIDGET
    elif not has_any_year("", text, cfg):
        category = CATEGORY_NO_YEAR
    else:
        category = CATEGORY_PARTIAL
    contexts: list[str] = []
    for mention in bare:
        context = mention_context(text, mention)
        if context not in contexts:
            contexts.append(context)
        if len(contexts) >= _CONTEXT_LIMIT:
            break
    return PageDating(
        page_id=page_id,
        title=title,
        url=url,
        status=status,
        category=category,
        date=date,
        basis=basis,
        bare_count=len(bare),
        contexts=contexts,
        explicit_count=len(explicit),
        explicit_years=sorted({m.year for m in explicit if m.year is not None}),
        excluded=dict(Counter(m.excluded for m in mentions if m.excluded)),
        bare=bare,
    )


def infer_pages(
    pages: Sequence[PageDating],
    announcements: Sequence[AnnouncementRef],
    all_titles: Iterable[str],
    cfg: DateSettings,
) -> list[PageDating]:
    """第一来源：同名公告。返回还没推断出来、要看页面创建时间的那些页面。"""
    pending: list[PageDating] = []
    if not cfg.infer_enabled:
        return pending
    titles = list(all_titles)
    for page in pages:
        if not page.needs_inference:
            continue
        page.inference = infer_from_announcements(page.title, page.bare, announcements, titles, cfg)
        if not page.inference.date:
            pending.append(page)
    return pending


def infer_from_histories(
    pending: Sequence[PageDating], histories: Mapping[int, PageHistory], cfg: DateSettings
) -> None:
    """第二来源：页面创建时间。"""
    for page in pending:
        first = page.inference.reason if page.inference else ""
        second = infer_from_created(page.bare, histories.get(page.page_id), cfg)
        if second.date:
            page.inference = second
        else:
            page.inference = Inference(reason="；".join(r for r in (first, second.reason) if r))


# ── 导出 ──────────────────────────────────────────────────────────────

_STATUS_NOTES = {
    "nav": "这一页是纯导航页，没有进知识库",
    "empty": "这一页清洗后几乎没有正文，没有进知识库",
    "broken": "这一页只渲染出一句报错，没有进知识库",
}


def csv_rows(pages: Iterable[PageDating]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for page in sorted((p for p in pages if p.listed), key=lambda p: p.title):
        inference = page.inference
        notes: list[str] = []
        basis = ""
        if page.category == CATEGORY_PARTIAL:
            years = "、".join(str(y) for y in page.explicit_years) or "只写了年、没写月日"
            notes.append(
                f"页面里另有 {page.explicit_count} 处日期带年份（{years}），"
                f"这 {page.bare_count} 处不带；页面自己的活动日期认不出来，没有推断"
            )
        elif inference is not None and inference.date:
            if inference.source == SOURCE_ANNOUNCEMENT:
                evidence = inference.evidence
                basis = (
                    f"同名公告：《{evidence.get('title', '')}》 {evidence.get('date', '')} "
                    f"{evidence.get('url', '')}"
                ).strip()
            else:
                basis = f"页面创建时间：{inference.evidence.get('created_at', '')}"
        elif inference is not None:
            notes.append(f"未能推断：{inference.reason}")
        if page.status in _STATUS_NOTES:
            notes.append(_STATUS_NOTES[page.status])
        skipped = "、".join(
            f"{reason} {count} 处"
            for reason, count in page.excluded.items()
            if reason != HISTORICAL
        )
        if skipped:
            notes.append(f"另有不需要年份的日期未计入（{skipped}）")
        rows.append(
            {
                "页面标题": page.title,
                "页面链接": page.url,
                "缺年份的日期原文（前几处）": " ｜ ".join(page.contexts),
                "缺年份的日期共几处": str(page.bare_count),
                "推断的年月": (inference.date or "") if inference else "",
                "推断依据": basis,
                "备注": "；".join(notes),
            }
        )
    return rows


def write_missing_year_csv(path: Path, pages: Iterable[PageDating]) -> int:
    """写「活动页缺年份」表格（UTF-8 带 BOM，Excel 直接能开）。返回行数。"""
    rows = csv_rows(pages)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return len(rows)
