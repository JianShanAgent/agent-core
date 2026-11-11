"""管线各阶段之间传递的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 切片类型。输出里用英文值，报告里用中文名
KIND_ENTRY = "entry"
KIND_ANNOUNCEMENT = "announcement"
KIND_STORY = "story"
KIND_DATA = "data"  # 由 Data 数据集生成的文档（皮肤、百鬼棋局图鉴、花合战对白、状态说明……）
KIND_LABELS = {
    KIND_ENTRY: "条目",
    KIND_ANNOUNCEMENT: "公告",
    KIND_STORY: "剧情",
    KIND_DATA: "数据",
}

BLOCK_HEADING = "heading"
BLOCK_PARA = "para"
BLOCK_LIST = "list"
BLOCK_TABLE = "table"
BLOCK_PRE = "pre"


@dataclass(slots=True)
class Table:
    """已转成 Markdown 行的表格。按行保存，拆表时每片都能重复表题和表头。"""

    header: list[str]  # 表头行 + 分隔线
    rows: list[str]
    caption: str = ""

    def prefix(self) -> str:
        """每一片开头都要带的部分：表题（如有）+ 表头。"""
        head = "\n".join(self.header)
        return f"{self.caption}\n\n{head}" if self.caption else head

    def render(self, rows: list[str] | None = None) -> str:
        return "\n".join([self.prefix(), *(self.rows if rows is None else rows)])


@dataclass(slots=True)
class Block:
    """文档里的一个块：标题、段落、列表、表格或预格式文本。"""

    kind: str
    text: str = ""
    #: 标题层级（1 起；页面名不算层级）
    level: int = 0
    #: 标题在页面里的锚点（只有 wiki 原生标题有），用来拼可跳转的链接
    anchor: str = ""
    #: 列表的顶层条目（各自带着嵌套子项），拆列表时以它为单位
    items: list[str] = field(default_factory=list)
    table: Table | None = None
    #: 与上一块之间只换一行（剧情对白），而不是空一行
    tight: bool = False

    def render(self) -> str:
        if self.kind == BLOCK_TABLE and self.table is not None:
            return self.table.render()
        if self.kind == BLOCK_LIST:
            return "\n".join(self.items)
        return self.text

    def size(self) -> int:
        return len(self.render())


@dataclass(slots=True)
class Document:
    """一篇待切片的文档：一个条目页面、一帖公告，或一章剧情。"""

    #: 跨修订稳定的标识：page:<页面ID> / wechat:<文章ID> / dashen:<帖子ID> / story:<剧情ID>:<章节ID>
    #: / data:<数据集>:<条目>
    doc_key: str
    kind: str
    title: str
    url: str
    source: str  # 来源说明，如「wiki 条目」「微信公众号·网易阴阳师手游·游戏情报」
    page_id: int | None = None
    rev_id: int | None = None
    #: 公告的发帖日期（YYYY-MM-DD）；条目能从标题或内容判断年份时带年份，判断不了为 None
    date: str | None = None
    #: 页面自己没写年份时推断出来的年月（YYYY-MM）和依据；页面写了年份就一定为空
    date_inferred: str | None = None
    date_source: dict[str, Any] | None = None
    blocks: list[Block] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def text_chars(self) -> int:
        return sum(b.size() for b in self.blocks if b.kind != BLOCK_HEADING)


@dataclass(slots=True)
class Chunk:
    chunk_id: str
    doc_key: str
    kind: str
    title: str
    section_path: list[str]
    #: 切片首行：「页面名 > 章节路径」（公告另带日期和来源）
    header: str
    body: str
    content_hash: str
    url: str
    source: str
    page_id: int | None = None
    rev_id: int | None = None
    date: str | None = None
    seq: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    #: 去重时并进来的其它出处（内容完全相同的切片）
    also_in: list[dict[str, Any]] = field(default_factory=list)
    #: 要不要拿去做嵌入。只有标题、没有正文的公告为 False（留着只为能按标题查到链接）
    embed: bool = True
    #: 推断出来的年月（YYYY-MM）和依据（``{"kind": "announcement" | "page_created", …}``）
    date_inferred: str | None = None
    date_source: dict[str, Any] | None = None
    #: 切片里提到的实体，类型见 entities.py。正式名命中是确定的：
    #: ``{"name": 正式名, "type": 类型, "certain": true}``；昵称命中只是候选：
    #: ``{"name": 正式名, "type": 类型, "certain": false, "via": 命中的昵称}``
    entities: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        """拿去做嵌入的全文：标题路径 + 正文。"""
        return f"{self.header}\n\n{self.body}"

    def to_json(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_key": self.doc_key,
            "type": self.kind,
            "page_id": self.page_id,
            "rev_id": self.rev_id,
            "title": self.title,
            "section_path": self.section_path,
            "date": self.date,
            "date_inferred": self.date_inferred,
            "date_source": self.date_source,
            "url": self.url,
            "source": self.source,
            "seq": self.seq,
            "chars": len(self.body),
            "content_hash": self.content_hash,
            "header": self.header,
            "text": self.text,
            "embed": self.embed,
            "entities": self.entities,
            "also_in": self.also_in,
            "meta": self.meta,
        }
