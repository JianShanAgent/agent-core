"""HTML 表格 → Markdown 表格，以及各种不规整表格的降级处理。

- 合并单元格（rowspan / colspan）：先展开成规整的网格。跨行的值在每一行都重复
  （每一行单独拿出来都要看得懂）；表头里跨列的值每列都重复，正文里跨列的值只留在第一格。
- 多行表头：同一列自上而下用「 / 」连起来。
- 没有表头行：一列的当作普通文字逐行输出；两列的当作「名称：值」列表；三列及以上把第一行当表头。
- 嵌套表格：由调用方处理（外层按排版用的表格展开，内层表格各自照常转换）。
- 单元格里的换行、列表：用 ``<br>`` 连在一格里；``|`` 转义。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lxml.html import HtmlElement

from jianshanagent.plugins.knowledge.models import Table

FORM_TABLE = "table"
FORM_KEY_VALUE = "key_value"
FORM_LINES = "lines"
FORM_EMPTY = "empty"

_MAX_SPAN = 200


@dataclass(slots=True)
class TableStats:
    total: int = 0
    merged_cells: int = 0  # 含 rowspan / colspan 的表格数
    nested: int = 0  # 含嵌套表格、外层按排版表格展开的个数
    headerless: int = 0  # 没有表头行、把第一行当表头的表格数
    key_value: int = 0  # 没有表头的两列表格，转成「名称：值」列表
    single_column: int = 0  # 没有表头的一列表格，转成普通文字
    empty: int = 0  # 清洗后没有内容的表格
    multi_header: int = 0  # 表头不止一行的表格数
    markdown_tables: int = 0  # 最终输出成 Markdown 表格的个数
    rows: int = 0  # 输出的表格正文行数
    placeholder_rows: int = 0  # 因为只有占位值（如「/」）而去掉的行数

    def add(self, other: TableStats) -> None:
        for name in self.__slots__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(slots=True)
class _Cell:
    text: str = ""
    header: bool = False
    origin: bool = False  # 是原始单元格的左上角（不是合并单元格展开出来的）


@dataclass(slots=True)
class TableResult:
    form: str
    table: Table | None = None
    lines: list[str] = field(default_factory=list)  # FORM_KEY_VALUE / FORM_LINES 的各行
    caption: str = ""


def _span(value: str | None) -> int:
    try:
        return max(1, min(_MAX_SPAN, int((value or "1").strip())))
    except ValueError:
        return 1


def _rows(table: HtmlElement) -> list[tuple[HtmlElement, bool]]:
    """表格的各行（只取这张表自己的，不进嵌套表格），以及它是否在 thead 里。"""
    rows: list[tuple[HtmlElement, bool]] = []
    for child in table:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.lower()
        if tag == "tr":
            rows.append((child, False))
        elif tag in ("thead", "tbody", "tfoot"):
            rows += [
                (tr, tag == "thead")
                for tr in child
                if isinstance(tr.tag, str) and tr.tag.lower() == "tr"
            ]
    return rows


def _expand(
    rows: list[tuple[HtmlElement, bool]], cell_text: Callable[[HtmlElement], str]
) -> tuple[list[list[_Cell]], list[bool], bool]:
    """把合并单元格展开成网格。返回（网格，每行是否表头行，是否出现过合并单元格）。"""
    grid: list[list[_Cell]] = []
    header_flags: list[bool] = []
    pending: dict[int, tuple[int, _Cell]] = {}  # 列号 →（还要向下占几行，要重复的内容）
    merged = False
    for tr, in_thead in rows:
        cells = [c for c in tr if isinstance(c.tag, str) and c.tag.lower() in ("td", "th")]
        queue = iter(cells)
        row: list[_Cell] = []
        col = 0
        while True:
            if col in pending:
                remaining, cell = pending[col]
                row.append(_Cell(cell.text, cell.header))
                if remaining > 1:
                    pending[col] = (remaining - 1, cell)
                else:
                    del pending[col]
                col += 1
                continue
            source = next(queue, None)
            if source is None:
                if pending and max(pending) > col:
                    row.append(_Cell())
                    col += 1
                    continue
                break
            rowspan, colspan = _span(source.get("rowspan")), _span(source.get("colspan"))
            merged = merged or rowspan > 1 or colspan > 1
            text = cell_text(source)
            is_header = source.tag.lower() == "th"
            for offset in range(colspan):
                shown = text if offset == 0 or is_header else ""
                row.append(_Cell(shown, is_header, origin=offset == 0))
                if rowspan > 1:
                    pending[col] = (rowspan - 1, _Cell(shown, is_header))
                col += 1
        originals = [c for c in row if c.origin]
        grid.append(row)
        header_flags.append(in_thead or (bool(originals) and all(c.header for c in originals)))
    return grid, header_flags, merged


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def one_line(text: str, sep: str = "；") -> str:
    """把多行文字并成一行：上一行已经以标点结尾就直接接上，否则用 ``sep`` 隔开。"""
    out = ""
    for line in (part.strip() for part in text.split("\n")):
        if not line:
            continue
        if not out:
            out = line
        elif out[-1] in "，。；：、！？,.;:!?…—":
            out += line
        else:
            out += sep + line
    return out


def _row_line(cells: list[str]) -> str:
    return "| " + " | ".join(cell or " " for cell in cells) + " |"


def build_table(
    table: HtmlElement,
    cell_text: Callable[[HtmlElement], str],
    stats: TableStats,
    *,
    placeholders: frozenset[str] = frozenset(),
) -> TableResult:
    """转换一张不含嵌套表格的表。``cell_text`` 把单元格转成文字（多行用换行符分隔）。"""
    stats.total += 1
    caption = ""
    for child in table:
        if isinstance(child.tag, str) and child.tag.lower() == "caption":
            caption = one_line(cell_text(child), " ")
            break

    grid, header_flags, merged = _expand(_rows(table), cell_text)
    if merged:
        stats.merged_cells += 1

    # 规整：补齐列数，去掉全空的行和列
    width = max((len(row) for row in grid), default=0)
    for row in grid:
        row += [_Cell() for _ in range(width - len(row))]
    keep_rows = [i for i, row in enumerate(grid) if any(c.text for c in row)]
    if placeholders and width >= 2:
        # 一行除第一格外全是「没有」的占位值（如语音表里的「/」）：这一行没有信息，去掉
        def is_placeholder(row: list[_Cell]) -> bool:
            values = [c.text.strip() for c in row[1:]]
            return any(v in placeholders for v in values) and all(
                not v or v in placeholders for v in values
            )

        dropped = [i for i in keep_rows if not header_flags[i] and is_placeholder(grid[i])]
        stats.placeholder_rows += len(dropped)
        keep_rows = [i for i in keep_rows if i not in set(dropped)]
    grid = [grid[i] for i in keep_rows]
    header_flags = [header_flags[i] for i in keep_rows]
    keep_cols = [c for c in range(width) if any(row[c].text for row in grid)]
    grid = [[row[c] for c in keep_cols] for row in grid]
    width = len(keep_cols)
    if not grid or width == 0:
        stats.empty += 1
        return TableResult(FORM_EMPTY, caption=caption)

    header_rows = 0
    while header_rows < len(grid) and header_flags[header_rows]:
        header_rows += 1

    if header_rows == 0:
        if width == 1:
            stats.single_column += 1
            return TableResult(
                FORM_LINES, lines=[one_line(row[0].text) for row in grid], caption=caption
            )
        if width == 2:
            stats.key_value += 1
            lines = []
            for row in grid:
                key, value = one_line(row[0].text), one_line(row[1].text)
                if key and value:
                    joiner = "" if key.endswith(("：", ":")) else "："
                    lines.append(f"- {key}{joiner}{value}")
                else:
                    lines.append(f"- {key or value}")
            return TableResult(FORM_KEY_VALUE, lines=lines, caption=caption)
        stats.headerless += 1
        header_rows = 1
    elif header_rows > 1:
        stats.multi_header += 1

    header: list[str] = []
    for col in range(width):
        parts: list[str] = []
        for row in grid[:header_rows]:
            text = one_line(row[col].text, " ")
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        header.append(_escape(" / ".join(parts)))
    body = [_row_line([_escape(c.text) for c in row]) for row in grid[header_rows:]]
    stats.markdown_tables += 1
    stats.rows += len(body)
    return TableResult(
        FORM_TABLE,
        table=Table(
            header=[_row_line(header), _row_line(["---"] * width)], rows=body, caption=caption
        ),
        caption=caption,
    )
