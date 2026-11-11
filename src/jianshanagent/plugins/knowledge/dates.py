"""条目的日期：先认页面自己写明的，认不出来、而正文又有「X月X日」时才去推断。

两步的先后是「自适应」的关键：wiki 那边把年份补进页面以后，重跑时第一步就能认出来，
第二步不再执行，输出里也就不再有推断值。

**第一步：页面自己写明的日期**（:func:`page_date`），按顺序取第一个成立的：

1. 标题里的 ``YYYYMM`` 或 ``YYYY``（如 ``花合战/202108``）。
2. 正文里紧跟在「活动时间」「开启时间」这类字样后面的完整日期
   （``2026年9月23日``、``2026-09-23``、``2026/9/23``、``2026.9.23``、``2026年9月``）。
   这类日期全页可能有好几处（各个子玩法的开放时间）：它们都落在三个月以内就取最早的那个；
   跨度更大说明这一页在罗列历届活动，不属于某一年，留空。
3. 正文开头的一个日期范围（「2026年7月22日维护后-8月18日23:59」）：带年份的日期后面紧跟连接符和
   另一个日期，中间没有断句。只在全页写明年份的日期都落在三个月以内时才认——页面别处还提到别的年份
   （「2019年首次上线」这种回顾）时拿不准哪个才是活动日期，留空。
4. 「活动时间：2026年」这种只写了年份的。

没有跟在时间字样后面、也不在正文开头的年份（更新记录、回顾、实装日期）一律不当作条目的日期。
游戏上线（``min_year``）以前的年份是正文顺带提到的历史，不算。范围的后半段没写年份的
（「2026年12月30日-1月5日」）跟着前半段走，不算缺年份。「生日：9月19日」这类本来就不带年份的也不算。

**第二步：推断**（:func:`infer_date`），只对「正文有不带年份的月日、全页（含标题）没有任何年份」的页面做：

1. 同名公告：标题里出现条目名的公告帖子里，月日和页面对得上（相差不超过一个月，而且对上的日期
   离公告发布不超过两个月）的那些，取最早一篇。
   对得上的公告分属不同年份时（每年都办的活动）拿不准是哪一年，不采用。
2. 页面创建时间：创建时间和页面里的月日对得上才采用；页面创建后又维护了半年以上的不采用
   （这种页面的正文多半已经不是创建时写的那一期）。

两步都不成立就留空，列进报告的「未能推断」。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from jianshanagent.plugins.knowledge.settings import DateSettings

SOURCE_ANNOUNCEMENT = "announcement"
SOURCE_PAGE_CREATED = "page_created"
SOURCE_LABELS = {SOURCE_ANNOUNCEMENT: "据同名公告推断", SOURCE_PAGE_CREATED: "据页面创建时间推断"}

_CST = timezone(timedelta(hours=8))
_M = r"(?:1[0-2]|0?[1-9])"
_D = r"(?:3[01]|[12]\d|0?[1-9])"
_Y = r"20[0-4]\d"
#: 依次是：2026年9月23日 ／ 2026-09-23、2026/9/23、2026.9.23 ／ 2026年9月 ／ 9月23日
_DATE_RE = re.compile(
    rf"(?<!\d)(?P<y1>{_Y})\s*年\s*(?P<m1>{_M})\s*月\s*(?P<d1>{_D})\s*[日号]"
    rf"|(?<![\d.])(?P<y2>{_Y})\s*(?P<sep>[-/.])\s*(?P<m2>{_M})\s*(?P=sep)\s*(?P<d2>{_D})(?![\d.])"
    rf"|(?<!\d)(?P<y3>{_Y})\s*年\s*(?P<m3>{_M})\s*月(?!\s*\d)"
    rf"|(?<!\d)(?P<m4>{_M})\s*月\s*(?P<d4>{_D})\s*[日号]"
)
_ANY_YEAR_RE = re.compile(rf"(?<!\d)({_Y})\s*年|(?<![\d.])({_Y})\s*[-/.]\s*{_M}(?!\d)")
HISTORICAL = "历史年份"
_TITLE_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)")
_TITLE_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_RANGE_RE = re.compile(r"[-~～—–－至到]")
_NAME_STRIP_RE = re.compile(r"[\s·・•‧.．「」『』《》【】〈〉“”\"'‘’!！?？,，。:：|丨｜、~～\-—]+")
#: 一个范围的后半段沿用前半段的年份：两个日期之间隔得很近、有连接符、没有断句
_INHERIT_GAP = 30
_BREAK_RE = re.compile(r"[\n。；;，,]")
_LEAD_RANGE_GAP = 12


@dataclass(frozen=True, slots=True)
class DateMention:
    """正文里的一处日期。``year`` 为 None 表示这一处没有写年份。"""

    month: int
    day: int | None
    year: int | None
    start: int
    end: int
    #: 没有写年份，但它是一个带年份的范围的后半段（「2026年7月22日-8月18日」里的 8月18日）
    inherited_year: int | None = None
    #: 不是活动日期的原因（生日、每年固定的日子……）；空串表示是普通日期
    excluded: str = ""

    @property
    def bare(self) -> bool:
        """缺年份、而且本该有年份的日期。"""
        return self.year is None and self.inherited_year is None and not self.excluded

    @property
    def known_year(self) -> int | None:
        return self.year if self.year is not None else self.inherited_year

    def as_date(self, year: int | None = None) -> date | None:
        year = year if year is not None else self.known_year
        if year is None:
            return None
        try:
            return date(year, self.month, self.day or 1)
        except ValueError:
            return None


def find_dates(text: str, cfg: DateSettings | None = None) -> list[DateMention]:
    """找出正文里的日期，标出哪些缺年份、哪些本来就不该带年份。"""
    cfg = cfg or DateSettings()
    before_re = _marker_re(cfg.non_event_markers, tail=True)
    after_re = _marker_re(cfg.non_event_markers, tail=False)
    mentions: list[DateMention] = []
    for match in _DATE_RE.finditer(text):
        groups = match.groupdict()
        year = next((int(groups[k]) for k in ("y1", "y2", "y3") if groups[k]), None)
        month = next(int(groups[k]) for k in ("m1", "m2", "m3", "m4") if groups[k])
        day = next((int(groups[k]) for k in ("d1", "d2", "d4") if groups[k]), None)
        inherited: int | None = None
        excluded = ""
        if year is not None and year < cfg.min_year:
            excluded = HISTORICAL  # 游戏上线前的年份：正文顺带提到的历史，不是活动日期
        elif year is None:
            previous = mentions[-1] if mentions else None
            gap = text[previous.end : match.start()] if previous else ""
            if (
                previous is not None
                and previous.known_year is not None
                and not previous.excluded
                and len(gap) <= _INHERIT_GAP
                and _RANGE_RE.search(gap)
                and not _BREAK_RE.search(gap)
            ):
                inherited = previous.known_year
                if (month, day or 1) < (previous.month, previous.day or 1):
                    inherited += 1  # 跨年的范围：12月24日-1月6日
            else:
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                head = text[max(line_start, match.start() - cfg.marker_window) : match.start()]
                tail = text[match.end() : match.end() + cfg.marker_window]
                if line_end != -1:
                    tail = tail[: max(0, line_end - match.end())]
                hit = (before_re.search(head) if before_re else None) or (
                    after_re.search(tail) if after_re else None
                )
                if hit:
                    excluded = hit.group(1)
        mentions.append(
            DateMention(month, day, year, match.start(), match.end(), inherited, excluded)
        )
    return mentions


def _marker_re(markers: Sequence[str], *, tail: bool) -> re.Pattern[str] | None:
    if not markers:
        return None
    alternatives = "|".join(re.escape(m) for m in markers)
    return re.compile(rf"({alternatives})[^\n]*$" if tail else rf"^[^\n]*?({alternatives})")


def has_any_year(title: str, text: str, cfg: DateSettings | None = None) -> bool:
    """标题或正文里有没有可能是活动年份的年份（早于 ``min_year`` 的不算）。"""
    floor = (cfg or DateSettings()).min_year
    if _TITLE_YEAR_RE.search(title):
        return True
    return any(int(a or b) >= floor for a, b in _ANY_YEAR_RE.findall(text))


def mention_context(text: str, mention: DateMention, *, width: int = 22) -> str:
    """一处日期连同它前后的一点文字（不跨行），给人核对用。"""
    line_start = text.rfind("\n", 0, mention.start) + 1
    line_end = text.find("\n", mention.end)
    line_end = len(text) if line_end == -1 else line_end
    start = max(line_start, mention.start - width)
    end = min(line_end, mention.end + width)
    snippet = text[start:end].strip().lstrip("-|# ").strip()
    return ("…" if start > line_start else "") + snippet + ("…" if end < line_end else "")


# ── 第一步：页面自己写明的日期 ────────────────────────────────────────


def _month(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def page_date(
    title: str,
    text: str,
    cfg: DateSettings | None = None,
    mentions: Sequence[DateMention] | None = None,
) -> tuple[str | None, str]:
    """页面自己写明的日期。返回（``YYYY`` 或 ``YYYY-MM``，依据）；认不出来返回 ``(None, "")``。

    依据：``title`` 标题；``content`` 紧跟时间字样的日期；``content_lead`` 正文开头的日期范围；
    ``ambiguous`` 表示页面写了好几个相距很远的活动日期，拿不准，留空。
    """
    cfg = cfg or DateSettings()
    month = _TITLE_MONTH_RE.search(title)
    if month:
        return f"{month.group(1)}-{month.group(2)}", "title"
    year = _TITLE_YEAR_RE.search(title)
    if year:
        return year.group(1), "title"

    mentions = find_dates(text, cfg) if mentions is None else mentions
    explicit = [
        (m, d)
        for m in mentions
        if m.year is not None and not m.excluded and (d := m.as_date()) is not None
    ]
    spread = timedelta(days=cfg.same_event_days)

    # 紧跟在「活动时间」这类字样后面的日期
    anchored: list[date] = []
    if cfg.time_keywords and explicit:
        keyword_re = re.compile("|".join(re.escape(k) for k in cfg.time_keywords))
        for keyword in keyword_re.finditer(text):
            for mention, value in explicit:
                if mention.start < keyword.end():
                    continue
                gap = text[keyword.end() : mention.start]
                if len(gap) <= cfg.keyword_window and "\n" not in gap:
                    anchored.append(value)
                break  # 只看这个字样后面的第一个日期
    if anchored:
        if max(anchored) - min(anchored) <= spread:
            return _month(min(anchored)), "content"
        return None, "ambiguous"

    # 正文开头的日期范围
    starts = {m.start: i for i, m in enumerate(mentions)}
    for mention, value in explicit:
        if mention.start > cfg.lead_chars:
            break
        # 必须真的是「日期 - 日期」：后面紧跟连接符和另一个日期，中间没有断句
        # （「2019年3月首次上线，沿用至今」不是范围）
        index = starts.get(mention.start, -1) + 1
        following = mentions[index] if 0 < index < len(mentions) else None
        gap = text[mention.end : following.start] if following else ""
        if (
            following is None
            or len(gap) > _LEAD_RANGE_GAP
            or not _RANGE_RE.search(gap)
            or _BREAK_RE.search(gap)
        ):
            continue
        everything = [d for _, d in explicit]
        if max(everything) - min(everything) <= spread:
            return _month(value), "content_lead"
        return None, "ambiguous"

    # 只写了年份的「活动时间：2026年」
    if cfg.time_keywords:
        years = set(
            re.findall(
                rf"(?:{'|'.join(re.escape(k) for k in cfg.time_keywords)})\s*[：:]\s*({_Y})\s*年",
                text,
            )
        )
        if len(years) == 1:
            return years.pop(), "content"
    return None, ""


# ── 第二步：推断 ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AnnouncementRef:
    """一篇公告里推断日期用得上的部分。"""

    doc_key: str
    title: str
    date: str  # YYYY-MM-DD
    url: str = ""
    #: 公告标题和正文里提到的月日
    month_days: tuple[tuple[int, int], ...] = ()
    #: 这篇公告本身在不在知识库里（来源被关掉的只当依据用）
    in_kb: bool = True


@dataclass(frozen=True, slots=True)
class PageHistory:
    created_at: str  # ISO 时间
    last_edited_at: str = ""


@dataclass(slots=True)
class Inference:
    """一个页面的日期推断结果。``date`` 为空表示没推断出来，``reason`` 说明为什么。"""

    date: str | None = None  # YYYY-MM
    source: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    basis: str = ""  # 给人看的一句话依据
    reason: str = ""

    def date_source(self) -> dict[str, Any] | None:
        return {"kind": self.source, **self.evidence} if self.date else None


def normalize_name(text: str) -> str:
    return _NAME_STRIP_RE.sub("", text)


def name_candidates(title: str, other_titles: Iterable[str], cfg: DateSettings) -> list[str]:
    """拿去公告标题里找的名字，按可靠程度排序：整个标题 → 上级页面名 → 「·」分开的各段。

    「·」分开的段如果本身是另一个页面的标题（「为崽而战·浮世之航」里的「为崽而战」），
    它是每年都办的系列名，不拿来找。太短的名字也不用。
    """
    others = {normalize_name(t) for t in other_titles if t != title}
    base = title.split("/", 1)[0]
    raw = [title.rsplit("/", 1)[-1] if "/" in title else title, base]
    for source in (title.rsplit("/", 1)[-1], base):
        parts = [p for p in re.split(r"[·・•‧]", source) if p.strip()]
        if len(parts) > 1:
            raw += [p for p in reversed(parts) if normalize_name(p) not in others]
    out: list[str] = []
    for name in [title, *raw]:
        key = normalize_name(name)
        if len(key) >= cfg.min_name_chars and key not in out:
            out.append(key)
    return out


def _near(month: int, day: int, anchor: date) -> date | None:
    """把一个月日放到离 ``anchor`` 最近的那一年。"""
    best: date | None = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs(candidate - anchor) < abs(best - anchor):
            best = candidate
    return best


def _resolve(
    mentions: Sequence[DateMention], anchor: date, evidence: Sequence[date], cfg: DateSettings
) -> tuple[date, date, date] | None:
    """页面里的月日和依据日期对不对得上。对得上返回（页面里最早的日期，对上的页面日期，对上的依据日期）。"""
    resolved = sorted(d for m in mentions if (d := _near(m.month, m.day or 1, anchor)) is not None)
    pairs = [(abs(p - e), p, e) for p in resolved for e in evidence]
    if not pairs:
        return None
    distance, page_day, evidence_day = min(pairs, key=lambda item: (item[0], item[1]))
    if distance > timedelta(days=cfg.match_window_days):
        return None
    if abs(page_day - anchor) > timedelta(days=cfg.event_span_days):
        return None  # 对上的日期离公告发布 / 页面创建太远（五月的公告提到十一月），不算同一期
    nearby = [d for d in resolved if abs(d - anchor) <= timedelta(days=cfg.event_span_days)]
    return (min(nearby) if nearby else page_day), page_day, evidence_day


def _md(value: date) -> str:
    return f"{value.month}月{value.day}日"


def infer_from_announcements(
    title: str,
    mentions: Sequence[DateMention],
    announcements: Sequence[AnnouncementRef],
    other_titles: Iterable[str],
    cfg: DateSettings,
) -> Inference:
    reasons: list[str] = []
    indexed = [(normalize_name(a.title), a) for a in announcements]
    for name in name_candidates(title, other_titles, cfg):
        matched: list[tuple[AnnouncementRef, date, date, date]] = []
        named = 0
        for norm_title, ann in indexed:
            if name not in norm_title:
                continue
            named += 1
            try:
                published = date.fromisoformat(ann.date)
            except ValueError:
                continue
            evidence = [d for m, dd in ann.month_days if (d := _near(m, dd, published))]
            hit = _resolve(mentions, published, evidence or [published], cfg)
            if hit:
                matched.append((ann, *hit))
        if not matched:
            if named:
                reasons.append(f"标题含「{name}」的公告有 {named} 篇，但月日和页面对不上")
            continue
        years = sorted({start.year for _, start, _, _ in matched})
        if len(years) > 1:
            reasons.append(
                f"标题含「{name}」且月日对得上的公告分属 {'、'.join(map(str, years))} 年，拿不准"
            )
            continue
        # 最早的一篇；同一天的优先取本身在知识库里的
        ann, start, page_day, evidence_day = min(
            matched, key=lambda item: (item[0].date, not item[0].in_kb, item[0].doc_key)
        )
        return Inference(
            date=_month(start),
            source=SOURCE_ANNOUNCEMENT,
            evidence={
                "doc_key": ann.doc_key,
                "title": ann.title,
                "date": ann.date,
                "url": ann.url,
                "matched_name": name,
                "in_kb": ann.in_kb,
            },
            basis=(
                f"公告《{ann.title}》（{ann.date}，{ann.url or ann.doc_key}）"
                f"提到 {_md(evidence_day)}，页面写 {_md(page_day)}"
            ),
        )
    return Inference(reason="；".join(reasons) or "没有标题里出现条目名的公告")


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_CST)
    except ValueError:
        return None


def infer_from_created(
    mentions: Sequence[DateMention], history: PageHistory | None, cfg: DateSettings
) -> Inference:
    if history is None:
        return Inference(reason="没有取到页面创建时间")
    created = _parse_time(history.created_at)
    if created is None:
        return Inference(reason="页面创建时间无法解析")
    edited = _parse_time(history.last_edited_at) if history.last_edited_at else None
    if edited is not None and edited - created > timedelta(days=cfg.max_maintained_days):
        return Inference(
            reason=(
                f"页面创建于 {created.date()}，最后编辑于 {edited.date()}，"
                "维护时间太长，创建时间不能代表正文里的日期"
            )
        )
    hit = _resolve(mentions, created.date(), [created.date()], cfg)
    if hit is None:
        return Inference(reason=f"页面创建于 {created.date()}，和正文里的月日对不上")
    start, page_day, _ = hit
    return Inference(
        date=_month(start),
        source=SOURCE_PAGE_CREATED,
        evidence={"created_at": created.date().isoformat()},
        basis=f"页面创建时间 {created.date()}，页面写 {_md(page_day)}",
    )


def infer_date(
    title: str,
    mentions: Sequence[DateMention],
    announcements: Sequence[AnnouncementRef],
    other_titles: Iterable[str],
    history: PageHistory | None,
    cfg: DateSettings,
) -> Inference:
    """先看同名公告，再看页面创建时间。``mentions`` 只传缺年份的那些。"""
    first = infer_from_announcements(title, mentions, announcements, other_titles, cfg)
    if first.date:
        return first
    second = infer_from_created(mentions, history, cfg)
    if second.date:
        return second
    return Inference(reason="；".join(r for r in (first.reason, second.reason) if r))


def month_days(text: str, limit: int = 30) -> tuple[tuple[int, int], ...]:
    """一段文字里提到的月日（公告用）。"""
    out: list[tuple[int, int]] = []
    for match in _DATE_RE.finditer(text):
        groups = match.groupdict()
        day = next((int(groups[k]) for k in ("d1", "d2", "d4") if groups[k]), None)
        if day is None:
            continue
        pair = (next(int(groups[k]) for k in ("m1", "m2", "m3", "m4") if groups[k]), day)
        if pair not in out:
            out.append(pair)
        if len(out) >= limit:
            break
    return tuple(out)
