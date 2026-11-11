"""把文档的块序列切成切片。

规则：

- 按标题切。每个切片首行是「页面名 > 章节路径」，正文里不再重复自己所在的标题。
- 一整节（含它的子节）不超过目标长度时整节作一片；相邻的小节合得进目标长度就并成一片，
  这时路径取它们的公共部分，各小节的标题以 Markdown 标题行留在正文里。
  跨一级章节的合并有限制（见 :func:`_merge`），免得大章节丢掉自己的路径。
- 一节的正文超过上限才拆：在块与块之间拆；单个块仍超过上限时，
  表格按行拆（**每片重复表题和表头**）、列表按条目拆、段落按句子拆。
  不把一行表格、一个列表条目或一个句子切成两半（一个句子本身超过上限时才按逗号、再不行按字数硬切，并计数）。
- 不设重叠：切点都在结构边界上，表头和标题路径已经补足上下文。

长度按字符数算，不含首行路径。目标 800、上限 1200 的理由：

- 这个站的内容以中文为主，800 字大约是 500～700 个 token，远低于嵌入模型的输入上限，
  又足够放下一个完整的技能说明、一段活动规则或一张三四十行的排名表；
- 回答时一般取前 5～8 片交给对话模型，800 字一片总量在 4000～6500 字，上下文和延迟都可接受；
- 再小（300～400）会把表格拆得很碎、每片大半是重复的表头；
  再大则一片里混进多个话题，向量变「平均」，召回变差。
- 上限比目标宽 50%，是为了让略超目标的表格、列表整块保留，而不是为了凑数硬拆出一小片尾巴。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from jianshanagent.plugins.knowledge.dates import SOURCE_LABELS
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_LIST,
    BLOCK_PARA,
    BLOCK_TABLE,
    KIND_ANNOUNCEMENT,
    Block,
    Chunk,
    Document,
)
from jianshanagent.plugins.knowledge.settings import ChunkSettings

_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]+[」』”’）)】》]*|[^。！？!?；;\n]+$")
_CLAUSE_RE = re.compile(r"[^，、,：:\s]*[，、,：:\s]+|[^，、,：:\s]+$")
_LEAD_IN_END = ("：", ":")


@dataclass(slots=True)
class ChunkStats:
    split_tables: int = 0  # 被按行拆开的表格数
    table_pieces: int = 0  # 拆出来的表格片数
    split_lists: int = 0
    split_paragraphs: int = 0
    hard_splits: int = 0  # 一个句子超过上限、只能按字数硬切的次数
    oversize_units: int = 0  # 单行表格 / 单个条目本身就超过上限、原样保留的个数
    merged_sections: int = 0  # 被并进相邻切片的小节数

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(slots=True)
class _Node:
    title: str = ""
    level: int = 0
    anchor: str = ""
    path: tuple[str, ...] = ()
    anchors: tuple[str, ...] = ()
    blocks: list[Block] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)


@dataclass(slots=True)
class _Unit:
    """一个待输出的切片：路径 + 正文。``whole`` 表示它是完整的一节（或几节），可以与相邻的并片。"""

    path: tuple[str, ...]
    anchors: tuple[str, ...]
    text: str
    whole: bool
    last_path: tuple[str, ...]
    #: 「平铺」的内容：页面开头的导言，或没有子节的一级小节（卡片列表里的一张卡片就是这种）
    flat: bool = False


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_blocks(blocks: list[Block]) -> str:
    parts: list[str] = []
    previous: Block | None = None
    for block in blocks:
        text = block.render()
        if not text:
            continue
        if previous is not None:
            parts.append("\n" if block.tight and previous.tight else "\n\n")
        parts.append(text)
        previous = block
    return "".join(parts)


def _heading_line(depth: int, title: str) -> str:
    """正文里的小节标题行。depth 是它在路径里的位置（1 起），页面名相当于一级标题。"""
    return f"{'#' * min(6, depth + 1)} {title}"


# ── 文档 → 章节树 ─────────────────────────────────────────────────────


def _build_tree(blocks: list[Block]) -> _Node:
    root = _Node()
    stack = [root]
    for block in blocks:
        if block.kind != BLOCK_HEADING:
            stack[-1].blocks.append(block)
            continue
        while len(stack) > 1 and stack[-1].level >= block.level:
            stack.pop()
        parent = stack[-1]
        node = _Node(
            title=block.text,
            level=block.level,
            anchor=block.anchor,
            path=(*parent.path, block.text),
            anchors=(*parent.anchors, block.anchor),
        )
        parent.children.append(node)
        stack.append(node)
    return root


def _render_subtree(node: _Node) -> str:
    parts = [render_blocks(node.blocks)]
    for child in node.children:
        body = _render_subtree(child)
        if body:
            parts.append(f"{_heading_line(len(child.path), child.title)}\n\n{body}")
    return "\n\n".join(p for p in parts if p)


# ── 拆 ────────────────────────────────────────────────────────────────


def _pack(units: list[str], limit: int, joiner: str) -> list[list[str]]:
    """把若干不可再分的单位按顺序装进若干组，每组不超过 limit（单个单位超过时自成一组）。"""
    groups: list[list[str]] = []
    size = 0
    for unit in units:
        extra = len(unit) + (len(joiner) if groups and groups[-1] else 0)
        if groups and groups[-1] and size + extra <= limit:
            groups[-1].append(unit)
            size += extra
        else:
            groups.append([unit])
            size = len(unit)
    # 最后一组太小时，从前一组挪一些过来，免得剩一小片尾巴
    while len(groups) >= 2 and len(groups[-2]) > 1:
        tail = sum(len(u) for u in groups[-1])
        head = sum(len(u) for u in groups[-2])
        moved = len(groups[-2][-1])
        if tail + moved > head - moved or tail + moved > limit:
            break
        groups[-1].insert(0, groups[-2].pop())
    return groups


def _split_text(text: str, cfg: ChunkSettings, stats: ChunkStats) -> list[str]:
    """按句子拆长段落；句子本身超长时按逗号，再不行按字数硬切。"""
    units: list[str] = []
    for sentence in _SENTENCE_RE.findall(text):
        if len(sentence) <= cfg.max_chars:
            units.append(sentence)
            continue
        for clause_group in _pack(_CLAUSE_RE.findall(sentence), cfg.target_chars, ""):
            clause = "".join(clause_group)
            if len(clause) <= cfg.max_chars:
                units.append(clause)
                continue
            stats.hard_splits += 1
            units += [
                clause[i : i + cfg.target_chars] for i in range(0, len(clause), cfg.target_chars)
            ]
    return ["".join(group).strip() for group in _pack(units, cfg.target_chars, "")]


def _split_block(block: Block, cfg: ChunkSettings, stats: ChunkStats) -> list[str]:
    """拆一个本身就超过上限的块。"""
    if block.kind == BLOCK_TABLE and block.table is not None:
        table = block.table
        prefix = table.prefix()
        budget = max(cfg.target_chars - len(prefix) - 1, cfg.target_chars // 4)
        groups = _pack(table.rows, budget, "\n")
        stats.split_tables += 1
        stats.table_pieces += len(groups)
        stats.oversize_units += sum(
            1 for row in table.rows if len(row) + len(prefix) > cfg.max_chars
        )
        return [table.render(rows) for rows in groups]
    if block.kind == BLOCK_LIST:
        stats.split_lists += 1
        pieces: list[str] = []
        for group in _pack(block.items, cfg.target_chars, "\n"):
            text = "\n".join(group)
            if len(text) > cfg.max_chars:  # 单个条目就超长：当段落按句子拆
                stats.oversize_units += 1
                pieces += _split_text(text, cfg, stats)
            else:
                pieces.append(text)
        return pieces
    stats.split_paragraphs += 1
    return _split_text(block.text, cfg, stats)


def _is_lead_in(block: Block) -> bool:
    """以冒号结尾的短段落（「奖励如下：」）要和后面的列表或表格待在一起。"""
    return block.kind == BLOCK_PARA and "\n" not in block.text and block.text.endswith(_LEAD_IN_END)


def split_blocks(blocks: list[Block], cfg: ChunkSettings, stats: ChunkStats) -> list[str]:
    """把一节的正文切成若干片：尽量装到目标长度，不超过上限。"""
    pieces: list[str] = []
    current: list[Block] = []

    def flush() -> None:
        if current:
            pieces.append(render_blocks(current))
            current.clear()

    for block in blocks:
        size = block.size()
        if size > cfg.max_chars:
            # 前面攒的内容很少（或只是一句「如下：」的引子）时，并到拆出来的第一片前面，不单独成片
            lead = ""
            if current and len(render_blocks(current)) < cfg.min_chars:
                lead = render_blocks(current)
                current.clear()
            elif current and _is_lead_in(current[-1]):
                lead = current.pop().text
            flush()
            parts = _split_block(block, cfg, stats)
            if lead and parts:
                parts[0] = f"{lead}\n\n{parts[0]}"
            pieces += parts
            continue
        current_size = len(render_blocks(current))
        if current and current_size + 2 + size > cfg.target_chars:
            # 当前这片还很小就先别断（宁可略超目标），否则会留下一小片；但不能超过上限
            small = current_size < cfg.min_chars and current_size + 2 + size <= cfg.max_chars
            if not small:
                carried = current.pop() if len(current) > 1 and _is_lead_in(current[-1]) else None
                flush()
                if carried is not None:
                    current.append(carried)
        current.append(block)
    flush()
    # 最后一片太小就并回前一片（不超过上限时）
    if len(pieces) >= 2 and len(pieces[-1]) < cfg.min_chars:
        if len(pieces[-2]) + 2 + len(pieces[-1]) <= cfg.max_chars:
            tail = pieces.pop()
            pieces[-1] = f"{pieces[-1]}\n\n{tail}"
    return [p for p in pieces if p.strip()]


# ── 章节树 → 切片 ─────────────────────────────────────────────────────


def _emit(node: _Node, cfg: ChunkSettings, stats: ChunkStats) -> list[_Unit]:
    whole_text = _render_subtree(node)
    if len(whole_text) <= cfg.target_chars:
        if not whole_text:
            return []
        flat = len(node.path) <= 1 and not node.children
        return [_Unit(node.path, node.anchors, whole_text, True, _last_path(node), flat)]
    units: list[_Unit] = []
    own = split_blocks(node.blocks, cfg, stats)
    for piece in own:
        units.append(
            _Unit(node.path, node.anchors, piece, len(own) == 1, node.path, flat=not node.path)
        )
    for child in node.children:
        units += _emit(child, cfg, stats)
    return units


def _last_path(node: _Node) -> tuple[str, ...]:
    while node.children:
        node = node.children[-1]
    return node.path


def _common_prefix(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    count = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        count += 1
    return count


def _headings(path: tuple[str, ...], start: int) -> str:
    return "\n\n".join(_heading_line(i + 1, path[i]) for i in range(start, len(path)))


def _merge(units: list[_Unit], cfg: ChunkSettings, stats: ChunkStats) -> list[_Unit]:
    """相邻的完整小节并片。

    - 同属一个一级章节的：合起来不超过目标长度就并；其中一片很小（不足并片阈值）时放宽到上限。
    - 不同一级章节的：两片都是「平铺」内容（没有子节的一级小节，如卡片列表里相邻的卡片）时，
      合起来不超过目标长度就并；否则只有两片都很小才并。
      这样一个带子节的大章节不会为了收留隔壁的零碎而丢掉自己的章节路径——
      宁可留一片小的，它自己的标题路径是准的。
    """
    out: list[_Unit] = []
    for unit in units:
        if out and out[-1].whole and unit.whole:
            prev = out[-1]
            keep = _common_prefix(prev.path, unit.path)
            head = _headings(prev.path, keep)  # 路径缩短后，前一片原来在路径里的标题要落到正文里
            mid = _headings(unit.path, _common_prefix(prev.last_path, unit.path))
            text = "\n\n".join(p for p in (head, prev.text, mid, unit.text) if p)
            sizes = (len(prev.text), len(unit.text))
            if keep >= 1:
                relaxed = min(sizes) < cfg.min_chars and sizes[0] <= cfg.target_chars
                allowed = True
            else:
                relaxed = False
                allowed = (prev.flat and unit.flat) or max(sizes) < cfg.min_chars
            if allowed and len(text) <= (cfg.max_chars if relaxed else cfg.target_chars):
                stats.merged_sections += 1
                out[-1] = _Unit(
                    prev.path[:keep],
                    prev.anchors[:keep],
                    text,
                    True,
                    unit.last_path,
                    flat=keep == 0 and prev.flat and unit.flat,
                )
                continue
        out.append(unit)
    return out


def make_header(doc: Document, path: tuple[str, ...] | list[str]) -> str:
    header = " > ".join([doc.title, *path])
    if doc.date_inferred:
        # 推断出来的年份要让读到这一片的人（和模型）知道它是推断的
        label = SOURCE_LABELS.get(str((doc.date_source or {}).get("kind")), "推断")
        header = f"{header}（时间：{doc.date_inferred}，{label}）"
    if doc.kind == KIND_ANNOUNCEMENT:
        extras = [f"发布于 {doc.date}" if doc.date else "", doc.source]
        header = " ｜ ".join([header, *(e for e in extras if e)])
    return header


def chunk_document(doc: Document, cfg: ChunkSettings, stats: ChunkStats) -> list[Chunk]:
    units = _merge(_emit(_build_tree(doc.blocks), cfg, stats), cfg, stats)
    chunks: list[Chunk] = []
    for seq, unit in enumerate(units):
        body = unit.text.strip()
        if not body:
            continue
        content_hash = sha256_hex(body)
        path = list(unit.path)
        anchor = next((a for a in reversed(unit.anchors) if a), "")
        url = f"{doc.url}#{quote(anchor.replace(' ', '_'), safe='')}" if anchor else doc.url
        # 稳定 ID：文档标识 + 章节路径 + 正文哈希。内容不变则 ID 不变；内容或所在章节变了就是新切片
        chunk_id = sha256_hex("\x1f".join([doc.doc_key, " > ".join(path), content_hash]))[:24]
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_key=doc.doc_key,
                kind=doc.kind,
                title=doc.title,
                section_path=path,
                header=make_header(doc, path),
                body=body,
                content_hash=content_hash,
                url=url,
                source=doc.source,
                page_id=doc.page_id,
                rev_id=doc.rev_id,
                date=doc.date,
                seq=seq,
                meta=dict(doc.meta),
                embed=not doc.meta.get("title_only"),
                date_inferred=doc.date_inferred,
                date_source=dict(doc.date_source) if doc.date_source else None,
            )
        )
    return chunks
