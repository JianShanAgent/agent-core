"""渲染后 HTML 的清洗：去噪声，并把这个站特有的组件改写成普通的标题和段落。

规则都来自对真实页面的观察（式神页、活动页、机制页、列表页、检索页、首页），沿用灰机官方知识库的约定：

- 注释、脚本、样式、表单控件、音视频：移除。
- 内联 ``display:none`` 和带 ``hidden`` 属性的元素：移除（用户看不到的内容）。
- class 命中 ``remove_classes`` 的元素：整块移除。包括官方约定的 ``kb-ignore``、目录、
  导航框（「式神录导航」在近 300 个式神页各有一份）、剧情页的上下章导航、
  悬浮提示的预载内容（技能描述里每个状态名后面都跟着一份完整说明，同一页重复多次，页面下方另有汇总列表）。
- 图片：移除。alt 是文件名或内部编号的直接丢；alt 是名字、而且附近文字里没有这个名字的
  （只用头像表示式神的地方）把名字留成文字。图注（缩略图说明、画廊文字）是普通文字，照常保留。
- 红链（未创建页面）：退化为纯文字；指向缺失文件的红链（文字是「文件:xxx.png」之类）移除。
- 标签页（``tabber``）：按钮文字提成小标题放到对应内容前面（如式神页的「传记一 … 传记五」），
  内容全是图片的标签页（皮肤立绘）只留下按钮文字的列表。按钮和内容数量对不上时
  （「活动一览」的「节日活动」没有内容），按两边共有的 ``data-*`` 属性配对。
  有的标签页没有按钮，名字写在内容块的属性上（MediaWiki 的 Tabber 扩展写在 ``title`` 里，
  按钮是浏览器里的脚本生成的）：配置了 ``tabber_label_attribute`` 就从那个属性取。
- ``badge_texts``：只有图标的徽章（对局阵容里的「胜」）换成文字，接到它标注的名字后面。
- ``heading_classes``：把本来就是小标题、但用 div 写的元素（技能名等）提成标题，切片路径能带上它。
- ``term_classes`` / ``prefix_classes``：把「名称 + 说明」并成一行，
  给只靠图标表达含义的数值补上文字说明。
- 页面末尾链接密集的块：按没有 class 的底部导航移除。

排版标签（b、i、strong、em、span）不在这里处理，转 Markdown 时只取文字。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import lxml.html
from lxml.html import HtmlElement

from jianshanagent.plugins.knowledge.settings import CleanSettings

#: 整个去掉的标签（连同内容）
_DROP_TAGS = frozenset(
    "script style noscript link meta input button select textarea iframe object embed audio video "
    "source track canvas svg map template head title".split()
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_FILE_NAME_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg|bmp|ico)$", re.IGNORECASE)
_FILE_LINK_RE = re.compile(r"^(文件|File|Image|图像|媒体文件|Media)\s*[:：]", re.IGNORECASE)
_ID_LIKE_RE = re.compile(r"[A-Za-z0-9_\-. ]+")
_SEPARATOR_RE = re.compile(r"[\s·•|｜/、,，→←↑↓▶▼▾►:：\-—()（）\[\]【】]+")

_SECTION_TITLE_CLASS = "kb-section-title"
ATTR_HEADING = "data-kb-heading"  # 值是标题层级
ATTR_INLINE = "data-kb-inline"  # 让块级元素按行内处理（与前后文字并成一行）
_ATTR_TAB_LABEL = "data-kb-tab-label"
_ATTR_TAB_LEVEL = "data-kb-tab-level"


@dataclass(slots=True)
class CleanStats:
    removed: Counter[str] = field(default_factory=Counter)  # 原因 → 移除的元素数
    images: int = 0
    images_with_real_alt: int = 0  # alt 不是文件名的图片（一并移除了，统计出来备查）
    red_links: int = 0
    tabbers: int = 0
    raw_text_chars: int = 0  # 清洗前的文字量（不含空白、脚本和样式）
    text_chars: int = 0  # 清洗后的文字量（不含空白）
    link_chars: int = 0  # 其中站内链接的文字量
    nav_text_chars: int = 0  # 去掉页面末尾导航块之前的文字量（判断纯导航页用）
    nav_link_chars: int = 0


def parse_html(html: str) -> HtmlElement:
    """解析 HTML 片段，返回内容根节点（有 ``mw-parser-output`` 就取它）。"""
    root = lxml.html.fragment_fromstring(html or "", create_parent="div")
    for child in root:
        if isinstance(child.tag, str) and "mw-parser-output" in _classes(child):
            if not (root.text or "").strip() and len(root) == 1:
                return child
    return root


def _classes(el: HtmlElement) -> list[str]:
    return (el.get("class") or "").split()


def _visible_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def clean(root: HtmlElement, settings: CleanSettings, *, wiki: bool = True) -> CleanStats:
    """就地清洗 ``root``。

    ``wiki=False`` 用于公告帖子自带的 HTML：只做通用清洗，不做站点组件改写。
    """
    stats = CleanStats()
    stats.raw_text_chars = _visible_len(
        "".join(root.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]"))
    )
    if wiki:
        _drop_linked_cards(root, settings, stats)
        _mark_tab_labels(root, settings, stats)
        _apply_badges(root, settings)
    _prune(root, settings, stats)
    if wiki:
        _insert_section_titles(root, settings)
        _promote_headings(root, settings)
        _apply_terms(root, settings)
        # 先量一次：判断「整页是不是纯导航」要用去掉末尾导航块之前的样子
        stats.nav_text_chars, stats.nav_link_chars = _measure(root)
        _drop_trailing_nav(root, settings, stats)
    stats.text_chars, stats.link_chars = _measure(root)
    return stats


# ── 检索页里重复的卡片、没有标题的区块 ────────────────────────────────


def _drop_linked_cards(root: HtmlElement, settings: CleanSettings, stats: CleanStats) -> None:
    """卡片头部的链接指向已有条目时，卡片内容在那个条目里有完整版本，移除；指向未创建页面（红链）的保留。

    例：「技能检索」把全站 953 个技能各渲染成一张卡片，其中九成以上在各式神页的技能一节里已有；
    只有阴阳师（晴明、神乐等）没有自己的页面，他们的技能只出现在这里。
    """
    for card_class, head_class in settings.linked_cards.items():
        for card in [el for el in root.iter() if isinstance(el.tag, str)]:
            if card_class not in _classes(card):
                continue
            head = next((el for el in card.iter() if head_class in _classes(el)), None)
            link = next(iter(head.iter("a")), None) if head is not None else None
            if link is not None and "new" not in _classes(link):
                stats.removed[f".{card_class}（指向已有条目）"] += 1
                card.drop_tree()
            elif head is not None:
                # 留下的卡片：头部（「出自 …」）在技能名之前，会被算进上一张卡片的小节；挪到卡片末尾
                tail, head.tail = head.tail, None
                parent = head.getparent()
                previous = head.getprevious()
                if tail and previous is not None:
                    previous.tail = (previous.tail or "") + tail
                elif tail:
                    parent.text = (parent.text or "") + tail
                parent.remove(head)
                card.append(head)


def _insert_section_titles(root: HtmlElement, settings: CleanSettings) -> None:
    """给页面上没有标题的区块补一个标题，免得它被算进上一个小节。"""
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        for cls in _classes(el):
            title = settings.section_titles.get(cls)
            if title and el.text_content().strip():
                heading = lxml.html.Element("div")
                heading.set("class", _SECTION_TITLE_CLASS)
                heading.text = title
                el.addprevious(heading)
                break


# ── 标签页 ────────────────────────────────────────────────────────────


def _owning_tabber(el: HtmlElement, tabber_class: str) -> HtmlElement | None:
    for ancestor in el.iterancestors():
        if tabber_class in _classes(ancestor):
            return ancestor
    return None


def _mark_tab_labels(root: HtmlElement, settings: CleanSettings, stats: CleanStats) -> None:
    """把每个标签页的名字记到对应的内容块上。

    名字是按钮的文字（按钮和内容按出现顺序一一对应）；配置了 ``tabber_label_attribute``、
    内容块上又带着这个属性的，名字是属性的值。
    必须在移除按钮之前做；真正插入小标题要等图片等噪声清掉之后（见 :func:`_promote_headings`）。
    """
    attribute = settings.tabber_label_attribute
    tabbers = [el for el in root.iter() if isinstance(el.tag, str) and _is_tabber(el, settings)]
    for tabber in tabbers:
        items, labels = [], []
        for el in tabber.iterdescendants():
            if not isinstance(el.tag, str):
                continue
            classes = _classes(el)
            if _owning_tabber(el, settings.tabber_class) is not tabber:
                continue
            if settings.tabber_item_class in classes:
                items.append(el)
            elif settings.tabber_label_class in classes:
                labels.append(el)
        if attribute and any(item.get(attribute) for item in items):
            texts = [" ".join((item.get(attribute) or "").split()) for item in items]
        else:
            if items and len(items) != len(labels):
                # 数量对不上（有的按钮没有对应内容）：改按按钮和内容共有的 data-* 属性配对
                labels = _labels_by_attribute(items, labels)
            if not items or len(items) != len(labels):
                continue  # 对不上就不加小标题，内容照常保留
            texts = [" ".join(_label_text(label).split()) for label in labels]
        stats.tabbers += 1
        named = any(texts)
        for index, (item, text) in enumerate(zip(items, texts, strict=True)):
            if not text and named:
                # 同一组里有的按钮有字、有的没字（如语音表只有第一套写了「初始」）：
                # 没字的补一个编号，免得它的内容被算到前一个标签页名下
                text = f"第{index + 1}组（原页面未命名）"
            item.set(_ATTR_TAB_LABEL, text)


def _labels_by_attribute(items: list[HtmlElement], labels: list[HtmlElement]) -> list[HtmlElement]:
    """按共有的 ``data-*`` 属性给每块内容找按钮（``data-event="2"`` 的内容配同值的按钮）。

    每块内容都恰好配到一个按钮才算数，否则返回空列表。
    """
    matched: list[HtmlElement] = []
    for item in items:
        pairs = {(k, v) for k, v in item.attrib.items() if k.startswith("data-") and v}
        found = [label for label in labels if pairs & set(label.attrib.items())]
        if len(found) != 1 or found[0] in matched:
            return []
        matched.append(found[0])
    return matched


def _label_text(el: HtmlElement) -> str:
    """标签页按钮的文字。按钮里指向缺失图片的红链（文字和按钮名一样）不算，否则名字会重复一遍。"""
    parts = [el.text or ""]
    for child in el:
        if isinstance(child.tag, str) and not _is_missing_file_link(child):
            parts.append(_label_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _is_missing_file_link(el: HtmlElement) -> bool:
    if el.tag.lower() != "a" or "new" not in _classes(el):
        return False
    return bool(
        _FILE_LINK_RE.match(el.text_content().strip()) or _FILE_LINK_RE.match(el.get("title") or "")
    )


def _is_tabber(el: HtmlElement, settings: CleanSettings) -> bool:
    return settings.tabber_class in _classes(el)


# ── 剪枝 ──────────────────────────────────────────────────────────────


def _removal_reason(el: HtmlElement, settings: CleanSettings, stats: CleanStats) -> str:
    tag = el.tag.lower()
    if tag in _DROP_TAGS:
        return f"<{tag}>"
    if tag == "img":
        stats.images += 1
        alt = _meaningful_alt(el)
        if alt:
            # 图片是这段内容里唯一带着名字的东西（如只用头像表示式神）：把名字留成文字
            stats.images_with_real_alt += 1
            el.tail = alt + (el.tail or "")
        return "<img>"
    for cls in _classes(el):
        if cls in settings.remove_classes:
            return f".{cls}"
    if el.get("hidden") is not None:
        return "[hidden]"
    if _DISPLAY_NONE_RE.search(el.get("style") or ""):
        # 没选中的标签页也是用 display:none 藏起来的，那是正文，不能删
        if settings.tabber_item_class not in _classes(el):
            return "display:none"
    if tag == "a" and "new" in _classes(el):
        stats.red_links += 1
        if _is_missing_file_link(el):
            return "缺失文件的红链"
    return ""


def _meaningful_alt(img: HtmlElement) -> str:
    """图片的 alt 值得留成文字时返回它：不是文件名、不是内部编号，而且附近的文字里没有同样的字。"""
    alt = " ".join((img.get("alt") or "").split())
    if not alt or _FILE_NAME_RE.search(alt) or _ID_LIKE_RE.fullmatch(alt):
        return ""
    # 往上找到第一个带文字的祖先（最多五层）：那里已经写着同样的字，图片就是重复的
    ancestor = img.getparent()
    for _ in range(5):
        if ancestor is None:
            break
        if ancestor.get(_ATTR_TAB_LABEL) == alt:
            return ""
        nearby = ancestor.text_content().strip()
        if nearby:
            return "" if alt in nearby else alt
        ancestor = ancestor.getparent()
    return alt


def _prune(el: HtmlElement, settings: CleanSettings, stats: CleanStats) -> None:
    for child in list(el):
        if not isinstance(child.tag, str):  # 注释、处理指令
            child.drop_tree()
            continue
        reason = _removal_reason(child, settings, stats)
        if reason:
            stats.removed[reason] += 1
            child.drop_tree()  # 保留元素后面的尾随文字
        else:
            _prune(child, settings, stats)


# ── 小标题 ────────────────────────────────────────────────────────────


def _promote_headings(root: HtmlElement, settings: CleanSettings) -> None:
    """按文档顺序给「提升为标题」的元素定层级，并把标签页按钮文字落成小标题。

    - ``heading_classes`` 的元素：层级 = 最近的原生标题层级 + 1；
    - 标签页的小标题：层级 = 标签页容器出现时的当前层级 + 1（同一组标签页层级相同）。
    """
    real_level = 0  # 最近一个原生标题（h1–h6）的层级
    last_level = 0  # 最近一个标题（含提升出来的）的层级
    empty_labels: dict[HtmlElement, list[str]] = {}
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        classes = _classes(el)
        if tag in _HEADING_TAGS:
            real_level = last_level = int(tag[1])
            continue
        if _is_tabber(el, settings):
            el.set(_ATTR_TAB_LEVEL, str(last_level + 1))
        label = el.get(_ATTR_TAB_LABEL)
        if label is not None:
            tabber = _owning_tabber(el, settings.tabber_class)
            level = int((tabber.get(_ATTR_TAB_LEVEL) if tabber is not None else "") or "1")
            if not label:
                continue
            if not el.text_content().strip():
                if tabber is not None:  # 内容只有图片的标签页：只记下按钮文字
                    empty_labels.setdefault(tabber, []).append(label)
                continue
            heading = lxml.html.Element("div")
            heading.set(ATTR_HEADING, str(level))
            heading.text = label
            if el.tag.lower() == "table":
                el.addprevious(heading)
            else:
                heading.tail = el.text
                el.text = None
                el.insert(0, heading)
            last_level = level
            continue
        depth = max((settings.heading_classes.get(c, 0) for c in classes), default=0)
        if _SECTION_TITLE_CLASS in classes:
            depth = 1
        if depth and el.text_content().strip():
            last_level = real_level + depth
            el.set(ATTR_HEADING, str(last_level))
    for tabber, labels in empty_labels.items():
        para = lxml.html.Element("p")
        para.text = "、".join(labels)
        tabber.insert(0, para)


# ── 名称 + 说明、前缀 ─────────────────────────────────────────────────


def _apply_terms(root: HtmlElement, settings: CleanSettings) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        classes = _classes(el)
        for cls in classes:
            prefix = settings.prefix_classes.get(cls)
            if prefix and el.text_content().strip():
                el.text = prefix + (el.text or "")
        if settings.inline_classes.intersection(classes):
            el.set(ATTR_INLINE, "1")
        if settings.term_classes.intersection(classes):
            text = el.text_content().strip()
            if not text:
                continue
            if not text.endswith(("：", ":")):
                _append_text(el, "：")
            el.set(ATTR_INLINE, "1")
            following = el.getnext()
            if following is not None and isinstance(following.tag, str):
                if following.tag.lower() in ("div", "span", "p") and not (el.tail or "").strip():
                    following.set(ATTR_INLINE, "1")


def _apply_badges(root: HtmlElement, settings: CleanSettings) -> None:
    """只有图标的徽章（对局里的「胜」）：把配置的文字接到它后面那个兄弟元素的末尾。

    要在移除图片之前做，否则徽章只剩空壳。例：``<div class="battle-win-badge"><img></div>
    <div>狐方</div>`` → 「狐方（胜）」。
    """
    if not settings.badge_texts:
        return
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        text = next(
            (settings.badge_texts[c] for c in _classes(el) if c in settings.badge_texts), ""
        )
        if not text or el.text_content().strip():
            continue
        target = el.getnext()
        while target is not None and not (
            isinstance(target.tag, str) and target.text_content().strip()
        ):
            target = target.getnext()
        if target is not None:
            _append_text(target, text)


def _append_text(el: HtmlElement, text: str) -> None:
    if len(el):
        last = el[-1]
        last.tail = (last.tail or "").rstrip() + text
    else:
        el.text = (el.text or "").rstrip() + text


# ── 页面末尾的导航块 ──────────────────────────────────────────────────


def link_density(el: HtmlElement) -> tuple[int, int, int]:
    """返回（站内链接数，链接文字量，去掉分隔符后的总文字量）。"""
    links = [a for a in el.iter("a") if (a.get("href") or "").startswith("/")]
    link_chars = sum(len(_SEPARATOR_RE.sub("", a.text_content())) for a in links)
    total = len(_SEPARATOR_RE.sub("", el.text_content()))
    return len(links), link_chars, total


def _drop_trailing_nav(root: HtmlElement, settings: CleanSettings, stats: CleanStats) -> None:
    """移除页面末尾链接密集的块。只在它是页面的一小部分时才移除：
    整页就是一个链接列表的（剧情目录、速度检索）不归这里管，由「纯导航页」的判定处理。"""
    page_chars = max(1, _visible_len(root.text_content()))
    while len(root):
        last = root[-1]
        if not isinstance(last.tag, str) or (last.tail or "").strip():
            return
        if not last.text_content().strip():
            last.drop_tree()  # 清洗后剩下的空壳
            continue
        if last.tag.lower() in _HEADING_TAGS:
            return
        count, link_chars, total = link_density(last)
        dense = total > 0 and link_chars / total >= settings.nav_block_link_ratio
        minor = _visible_len(last.text_content()) / page_chars <= settings.nav_block_max_share
        if count >= settings.nav_block_min_links and dense and minor:
            stats.removed["页面末尾的导航块"] += 1
            last.drop_tree()
            continue
        return


def _measure(root: HtmlElement) -> tuple[int, int]:
    """返回（文字量，其中站内链接的文字量），都不含空白。"""
    links = sum(
        _visible_len(a.text_content())
        for a in root.iter("a")
        if (a.get("href") or "").startswith("/")
    )
    return _visible_len(root.text_content()), links
