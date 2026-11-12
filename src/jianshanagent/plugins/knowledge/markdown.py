"""清洗后的 HTML → 块序列（标题、段落、列表、表格），每块的文字都是 Markdown。

约定：

- 标题层级、列表层次、链接文字保留；链接地址不保留（切片是拿去做嵌入的，地址是噪声）。
- 排版用标签（b、i、strong、em、span 等）只取文字。
- ``<br>`` 是换行；连续两个换行是分段。``white-space: pre*`` 的元素里，源码里的换行也算换行。
- 相邻的两个「带 class 的 span」或链接之间没有文字时补一个空格：这个站大量用并排的 span
  摆放「名称 数值」「标签 标签」，直接连起来会粘成一串。
- 表格见 :mod:`tables`；含嵌套表格的表、只有一列的表按排版表格展开，逐格处理。
- 紧挨在表格前面的短句（不以句末标点结尾）当作表题并进表格，拆表时随表头一起重复。
"""

from __future__ import annotations

import re
from typing import Any

from lxml.html import HtmlElement

from jianshanagent.plugins.knowledge.cleaner import ATTR_HEADING, ATTR_INLINE
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_LIST,
    BLOCK_PARA,
    BLOCK_PRE,
    BLOCK_TABLE,
    Block,
)
from jianshanagent.plugins.knowledge.tables import (
    FORM_EMPTY,
    FORM_KEY_VALUE,
    FORM_LINES,
    FORM_TABLE,
    TableStats,
    build_table,
    is_single_column,
    one_line,
)

_BLOCK_TAGS = frozenset(
    "address article aside blockquote caption center dd details dialog div dl dt fieldset "
    "figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr li main nav ol p pre "
    "section summary table tbody td tfoot th thead tr ul".split()
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "rp", "img"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_PRE_RE = re.compile(r"white-space\s*:\s*pre", re.IGNORECASE)
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")
_WHITESPACE_RE = re.compile(r"\s+")
#: 零宽字符去掉；各种不换行空格、全角空格按普通空格处理
_CHAR_MAP = {
    0x200B: None,
    0x200C: None,
    0x200D: None,
    0x2060: None,
    0xFEFF: None,
    0x00AD: None,
    0x00A0: " ",
    0x3000: " ",
    0x2002: " ",
    0x2003: " ",
    0x2009: " ",
}
_NO_SPACE_AFTER = "：:（(【「『《〈、，“‘"
_SENTENCE_END = "。！？!?；;，,、…"


def normalize_text(text: str) -> str:
    return text.translate(_CHAR_MAP)


class _Inline:
    """行内文字缓冲：收集文字和换行，输出时整理空白。"""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def add(self, text: str | None, pre: bool = False) -> None:
        if not text:
            return
        text = normalize_text(text)
        text = _SPACES_RE.sub(" ", text) if pre else _WHITESPACE_RE.sub(" ", text)
        if text:
            self.parts.append(text)

    def newline(self) -> None:
        self.parts.append("\n")

    def space(self) -> None:
        if self.parts:
            last = self.parts[-1][-1]
            if not last.isspace() and last not in _NO_SPACE_AFTER:
                self.parts.append(" ")

    def clear(self) -> None:
        self.parts.clear()

    def text(self) -> str:
        lines = [_SPACES_RE.sub(" ", line).strip() for line in "".join(self.parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _is_pre(el: HtmlElement) -> bool:
    return bool(_PRE_RE.search(el.get("style") or ""))


def _tag(el: HtmlElement) -> str:
    return el.tag.lower() if isinstance(el.tag, str) else ""


def _spaced_kind(el: HtmlElement) -> bool:
    tag = _tag(el)
    return (
        tag == "a" or (tag == "span" and bool(el.get("class"))) or el.get(ATTR_INLINE) is not None
    )


def _needs_space(el: HtmlElement) -> bool:
    if not _spaced_kind(el):
        return False
    prev = el.getprevious()
    return prev is not None and _spaced_kind(prev) and not (prev.tail or "").strip()


class BlockBuilder:
    def __init__(
        self, *, label_max_chars: int = 40, table_placeholders: frozenset[str] = frozenset()
    ):
        self.blocks: list[Block] = []
        self.table_stats = TableStats()
        self.label_max_chars = label_max_chars
        self.table_placeholders = table_placeholders

    # ── 入口 ──────────────────────────────────────────────────────────

    def feed(self, root: HtmlElement) -> None:
        self._container(root, False)

    def finish(self) -> list[Block]:
        self._attach_table_labels()
        return self.blocks

    # ── 容器与分发 ────────────────────────────────────────────────────

    def _is_block(self, el: HtmlElement) -> bool:
        if el.get(ATTR_INLINE) is not None:
            return False
        if _tag(el) in _BLOCK_TAGS or el.get(ATTR_HEADING):
            return True
        # 行内元素里包着块级内容（如链接包着 div）：按容器处理
        return any(_tag(d) in _BLOCK_TAGS for d in el.iterdescendants())

    def _container(self, el: HtmlElement, pre: bool) -> None:
        pre = pre or _is_pre(el)
        buf = _Inline()
        buf.add(el.text, pre)
        for child in el:
            if isinstance(child.tag, str):
                self._node(child, buf, pre)
            buf.add(child.tail, pre)
        self._flush(buf)

    def _node(self, el: HtmlElement, buf: _Inline, pre: bool) -> None:
        tag = _tag(el)
        if tag == "br":
            buf.newline()
        elif tag in _SKIP_TAGS:
            return
        elif self._is_block(el):
            self._flush(buf)
            self._block(el, tag, pre)
        else:
            self._inline(el, buf, pre)

    def _flush(self, buf: _Inline) -> None:
        text = buf.text()
        buf.clear()
        for para in text.split("\n\n"):
            if para.strip():
                self.blocks.append(Block(BLOCK_PARA, text=para.strip()))

    def _block(self, el: HtmlElement, tag: str, pre: bool) -> None:
        level = el.get(ATTR_HEADING)
        if level:
            self._heading(el, int(level))
        elif tag in _HEADING_TAGS:
            self._heading(el, int(tag[1]))
        elif tag in ("ul", "ol"):
            self._list(el, pre)
        elif tag == "dl":
            self._definition_list(el, pre)
        elif tag == "table":
            self._table(el, pre)
        elif tag == "pre":
            text = normalize_text(el.text_content()).strip("\n")
            if text.strip():
                self.blocks.append(Block(BLOCK_PRE, text=text))
        elif tag != "hr":
            self._container(el, pre)

    # ── 行内 ──────────────────────────────────────────────────────────

    def _inline(self, el: HtmlElement, buf: _Inline, pre: bool, *, flat: bool = False) -> None:
        """把元素当行内内容收进缓冲。``flat`` 时遇到块级后代用换行隔开（单元格、列表项里用）。"""
        tag = _tag(el)
        if tag == "br":
            buf.newline()
            return
        if not tag or tag in _SKIP_TAGS:
            return
        pre = pre or _is_pre(el)
        breaks = flat and tag in _BLOCK_TAGS and el.get(ATTR_INLINE) is None
        if breaks:
            buf.newline()
            if tag == "li":
                buf.add("· ")
        elif _needs_space(el):
            buf.space()
        if tag == "rt":
            buf.add("（")
        buf.add(el.text, pre)
        for child in el:
            self._inline(child, buf, pre, flat=flat)
            buf.add(child.tail, pre)
        if tag == "rt":
            buf.add("）")
        if breaks:
            buf.newline()

    def _text(self, el: HtmlElement, pre: bool = False) -> str:
        """元素的文字（多行用换行符分隔，块级后代各占一行）。"""
        buf = _Inline()
        pre = pre or _is_pre(el)
        buf.add(el.text, pre)
        for child in el:
            self._inline(child, buf, pre, flat=True)
            buf.add(child.tail, pre)
        return "\n".join(line for line in buf.text().split("\n") if line.strip())

    def _heading(self, el: HtmlElement, level: int) -> None:
        text = one_line(self._text(el), " ")
        if not text:
            return
        anchor = ""
        for span in el.iter("span"):
            if "mw-headline" in (span.get("class") or "").split():
                anchor = span.get("id") or ""
                break
        self.blocks.append(Block(BLOCK_HEADING, text=text, level=max(1, level), anchor=anchor))

    # ── 列表 ──────────────────────────────────────────────────────────

    def _list(self, el: HtmlElement, pre: bool) -> None:
        items: list[str] = []

        def flush_items() -> None:
            if items:
                self.blocks.append(Block(BLOCK_LIST, items=list(items)))
                items.clear()

        ordered = _tag(el) == "ol"
        start = el.get("start") or "1"
        index = int(start) if start.lstrip("-").isdigit() else 1
        stray = _Inline()  # 列表里不在 li 中的零散内容，当作一条
        stray.add(el.text, pre)

        def flush_stray() -> None:
            for line in stray.text().split("\n"):
                if line.strip():
                    items.append(f"- {line.strip()}")
            stray.clear()

        for child in el:
            tag = _tag(child)
            if tag == "li":
                flush_stray()
                marker = f"{index}. " if ordered else "- "
                index += 1
                for kind, value in self._list_item(child, pre):
                    if kind == "lines":
                        items.append(_join_item(marker, value))
                        marker = "- "
                    else:  # 列表项里的表格：先把已有的条目输出，再输出表格
                        flush_items()
                        self._table(value, pre)
            elif tag in ("ul", "ol"):
                flush_stray()
                nested = self._nested_items(child, pre)
                if nested and items:
                    items[-1] += "\n" + "\n".join(nested)
                else:
                    items.extend(line.strip() for line in nested)
            elif tag:
                self._inline(child, stray, pre, flat=True)
            stray.add(child.tail, pre)
        flush_stray()
        flush_items()

    def _list_item(self, li: HtmlElement, pre: bool) -> list[tuple[str, Any]]:
        """列表条目按出现顺序拆成若干段：("lines", 文字行) 或 ("table", 表格元素)。"""
        segments: list[tuple[str, Any]] = []
        buf = _Inline()
        buf.add(li.text, pre)
        nested: list[str] = []

        def flush_text() -> None:
            lines = [line for line in buf.text().split("\n") if line.strip()]
            buf.clear()
            if not lines and nested:  # 只有子列表的条目：第一个子项顶上来当正文
                lines = [re.sub(r"^(- |\d+\. )", "", nested[0].strip()), *nested[1:]]
            else:
                lines += nested
            nested.clear()
            if lines:
                segments.append(("lines", lines))

        for child in li:
            tag = _tag(child)
            if tag in ("ul", "ol"):
                nested.extend(self._nested_items(child, pre))
            elif tag == "table":
                flush_text()
                segments.append(("table", child))
            elif tag:
                self._inline(child, buf, pre, flat=True)
            buf.add(child.tail, pre)
        flush_text()
        return segments

    def _nested_items(self, el: HtmlElement, pre: bool) -> list[str]:
        """子列表的各行，已经缩进两格。子列表里的表格按文字处理。"""
        lines: list[str] = []
        ordered = _tag(el) == "ol"
        index = 1
        for child in el:
            if _tag(child) != "li":
                continue
            marker = f"{index}. " if ordered else "- "
            index += 1
            own: list[str] = []
            for kind, value in self._list_item(child, pre):
                if kind == "lines":
                    own += value
                else:
                    own += [line for line in self._text(value, pre).split("\n") if line.strip()]
            if own:
                lines += ["  " + line for line in _join_item(marker, own).split("\n")]
        return lines

    def _definition_list(self, el: HtmlElement, pre: bool) -> None:
        """dt / dd 成对输出为「- 名称：内容」；内容为空的一对整个略去。"""
        items: list[str] = []
        term = ""

        def flush_items() -> None:
            if items:
                self.blocks.append(Block(BLOCK_LIST, items=list(items)))
                items.clear()

        def lone(text: str) -> None:
            flush_items()
            self.blocks.append(Block(BLOCK_PARA, text=text))

        for node in el.iter("dt", "dd"):
            owner = next((a for a in node.iterancestors() if _tag(a) == "dl"), None)
            if owner is not el:
                continue
            if _tag(node) == "dt":
                if term:
                    lone(term)
                term = one_line(self._text(node, pre), " ")
                continue
            if any(_tag(d) in ("table", "ul", "ol", "dl") for d in node.iterdescendants()):
                if term:
                    lone(term)
                    term = ""
                flush_items()
                self._container(node, pre)
                continue
            text = one_line(self._text(node, pre))
            if text and term:
                joiner = "" if term.endswith(("：", ":")) else "："
                items.append(f"- {term}{joiner}{text}")
            elif text:
                lone(text)
            term = ""
        if term:
            lone(term)
        flush_items()

    # ── 表格 ──────────────────────────────────────────────────────────

    def _table(self, el: HtmlElement, pre: bool) -> None:
        nested = any(_tag(d) == "table" for d in el.iterdescendants())
        if nested or is_single_column(el):
            # 排版用的表格，逐格当容器处理。嵌套表格：外层是排版用的，内层表格各自转换；
            # 只有一列的表格：给一段内容加的框，表头成了它前面的一行
            self.table_stats.total += 1
            if nested:
                self.table_stats.nested += 1
            else:
                self.table_stats.single_column += 1
            for cell in el.iter("caption", "td", "th"):
                owner = next((a for a in cell.iterancestors() if _tag(a) == "table"), None)
                if owner is el:
                    self._container(cell, pre)
            return
        result = build_table(
            el,
            lambda cell: self._text(cell, pre),
            self.table_stats,
            placeholders=self.table_placeholders,
        )
        if result.form == FORM_EMPTY:
            return
        if result.form == FORM_TABLE and result.table is not None:
            self.blocks.append(Block(BLOCK_TABLE, table=result.table))
            return
        if result.caption:
            self.blocks.append(Block(BLOCK_PARA, text=result.caption))
        if result.form == FORM_KEY_VALUE:
            self.blocks.append(Block(BLOCK_LIST, items=result.lines))
        elif result.form == FORM_LINES:
            self.blocks += [Block(BLOCK_PARA, text=line) for line in result.lines if line]

    def _attach_table_labels(self) -> None:
        """表格前紧挨着的短句当作表题并进表格。"""
        out: list[Block] = []
        for block in self.blocks:
            previous = out[-1] if out else None
            if (
                block.kind == BLOCK_TABLE
                and block.table is not None
                and not block.table.caption
                and previous is not None
                and previous.kind == BLOCK_PARA
                and self._is_label(previous.text)
            ):
                block.table.caption = previous.text
                out.pop()
            out.append(block)
        self.blocks = out

    def _is_label(self, text: str) -> bool:
        return (
            "\n" not in text
            and 0 < len(text) <= self.label_max_chars
            and text[-1] not in _SENTENCE_END
        )


def _join_item(marker: str, lines: list[str]) -> str:
    """列表条目：第一行带标记，后续行（续行和子项）缩进两格对齐。"""
    first, *rest = lines
    out = [marker + first]
    for line in rest:
        out.append(line if line.startswith("  ") else "  " + line)
    return "\n".join(out)


def html_to_blocks(
    root: HtmlElement,
    *,
    label_max_chars: int = 40,
    table_placeholders: frozenset[str] = frozenset(),
) -> tuple[list[Block], TableStats]:
    builder = BlockBuilder(label_max_chars=label_max_chars, table_placeholders=table_placeholders)
    builder.feed(root)
    return builder.finish(), builder.table_stats
