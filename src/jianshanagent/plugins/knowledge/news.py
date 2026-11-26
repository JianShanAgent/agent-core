"""近期公告查询：只按可靠发布日期排不同文档，不参与向量相关性排名。"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jianshanagent.plugins.knowledge.store import KbChunk


MAX_NEWS_LIMIT = 100
# 两种存储使用相同的 ISO 形状；拒绝 now、无时区、闰秒及宽松日期。
# 支持 Z 和 ±HH:MM/±HHMM（实际民用时区范围 ±14:00），最多微秒。
PUBLISHED_AT_PATTERN = (
    "[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    "([.][0-9]{1,6})?(Z|[+-](0[0-9]|1[0-3]):?[0-5][0-9]|[+-]14:?00)"
)


def _published_at(value: object) -> datetime | None:
    if not isinstance(value, str) or re.fullmatch(PUBLISHED_AT_PATTERN, value) is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso_date(value: str | None) -> str | None:
    """不接受推断年月、时间戳或宽松日期格式；坏的历史数据按无日期处理。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


@dataclass(frozen=True, slots=True)
class NewsQuery:
    source_platforms: tuple[str, ...]
    date_from: str | None = None
    date_to: str | None = None
    as_of: str | None = None
    keywords: tuple[str, ...] = ()
    limit: int = 10

    def __post_init__(self) -> None:
        for name in ("date_from", "date_to", "as_of"):
            value = getattr(self, name)
            if value is not None and _iso_date(value) is None:
                raise ValueError(f"{name} 必须是有效的 YYYY-MM-DD 日期")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from 不能晚于 date_to")
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_NEWS_LIMIT:
            raise ValueError(f"limit 必须是 1..{MAX_NEWS_LIMIT} 的整数")
        for name in ("source_platforms", "keywords"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{name} 必须是非空字符串的序列")
            object.__setattr__(self, name, tuple(dict.fromkeys(v.strip() for v in values)))


@dataclass(frozen=True, slots=True)
class NewsResult:
    chunks: list[KbChunk]
    #: 同来源、已挂载公告截至 as_of 的最新发布日期，不受主题或用户日期区间影响。
    source_latest_date: str | None = None
    #: 日期及主题筛选后的总篇数（应用 limit 之前）。
    matching_documents: int = 0
    #: 同来源、已挂载公告中没有可靠发布日期的篇数，不受主题或用户日期区间影响。
    undated_documents: int = 0


def select_recent_documents(
    chunks: Iterable[KbChunk],
    query: NewsQuery,
    *,
    exclude_wikis: Collection[str] | None = None,
) -> NewsResult:
    """内存实现；与 PG 的文档级查询保持同义，先聚合再筛主题、日期及限篇数。"""
    if not query.source_platforms:
        return NewsResult(chunks=[])
    sources = frozenset(query.source_platforms)
    excluded = frozenset(exclude_wikis or ())
    as_of = query.as_of or date.today().isoformat()
    documents: dict[str, list[KbChunk]] = {}
    for chunk in chunks:
        platform = chunk.meta.get("source_platform")
        if (
            chunk.kind == "announcement"
            and isinstance(platform, str)
            and platform in sources
            and chunk.wiki not in excluded
        ):
            documents.setdefault(chunk.doc_key, []).append(chunk)

    latest: str | None = None
    undated = 0
    matching: list[tuple[str, datetime | None, KbChunk]] = []
    keywords = tuple(word.lower() for word in query.keywords)
    for parts in documents.values():
        published = max((_iso_date(part.date) or "" for part in parts), default="")
        if not published:
            undated += 1
            continue
        if published > as_of:
            continue
        latest = max(latest or "", published)
        if query.date_from and published < query.date_from:
            continue
        if query.date_to and published > query.date_to:
            continue
        if keywords:
            texts = [text.lower() for part in parts for text in (part.title, part.text)]
            if not all(any(word in text for text in texts) for word in keywords):
                continue
        first = min(parts, key=lambda part: (part.seq, part.chunk_id))
        published_at = max(
            (stamp for part in parts if (stamp := _published_at(part.meta.get("published_at")))),
            default=None,
        )
        # 仅近期结果副本补正文档日期；原始切片与普通检索不变。
        matching.append((published, published_at, replace(first, date=published)))

    # 日期优先；同日有可靠时刻的按实际时刻降序，其余及等时刻按文档键稳定排序。
    matching.sort(key=lambda item: (item[2].doc_key, item[2].chunk_id))
    matching.sort(key=lambda item: (item[1] is not None, item[1]), reverse=True)
    matching.sort(key=lambda item: item[0], reverse=True)
    return NewsResult(
        chunks=[first for _, _, first in matching[: query.limit]],
        source_latest_date=latest,
        matching_documents=len(matching),
        undated_documents=undated,
    )
