"""知识库的清洗和转块：标签页的名字写在属性上的、只有一列的表格。

（按钮式的标签页、合并单元格、没有表头的表格这些，测试在阴阳师插件那边：用的是那个站的页面。）
"""

from __future__ import annotations

from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document
from jianshanagent.plugins.knowledge.cleaner import clean, parse_html
from jianshanagent.plugins.knowledge.markdown import html_to_blocks
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_PARA,
    BLOCK_TABLE,
    KIND_ENTRY,
    Block,
    Document,
)
from jianshanagent.plugins.knowledge.settings import ChunkSettings, CleanSettings
from jianshanagent.plugins.knowledge.tables import TableStats

#: MediaWiki 的 Tabber 扩展：没有按钮，标签页的名字在内容块的 title 属性上
TABBER_EXTENSION = CleanSettings(tabber_item_class="tabbertab", tabber_label_attribute="title")


def _convert(html: str, settings: CleanSettings) -> tuple[list[Block], TableStats]:
    root = parse_html(html)
    clean(root, settings)
    return html_to_blocks(root)


def _blocks(html: str, settings: CleanSettings = TABBER_EXTENSION) -> list[Block]:
    return _convert(html, settings)[0]


def _headings(blocks: list[Block]) -> list[tuple[int, str]]:
    return [(b.level, b.text) for b in blocks if b.kind == BLOCK_HEADING]


def _paragraphs(blocks: list[Block]) -> list[str]:
    return [b.text for b in blocks if b.kind == BLOCK_PARA]


VOICES = """
    <h2><span class="mw-headline" id="式神语音">式神语音</span></h2>
    <div class="tabber">
      <div class="tabbertab" title="觉醒前 "><p>登场：迎接我的狂风吧</p></div>
      <div class="tabbertab" title="觉醒后" style="display:none"><p>登场：风，听我号令</p></div>
    </div>
"""


def test_tab_labels_come_from_an_attribute_of_the_tab() -> None:
    blocks = _blocks(VOICES)
    # 名字提成了小标题，在所在章节的下一级；属性值两头的空白不算
    assert _headings(blocks) == [(2, "式神语音"), (3, "觉醒前"), (3, "觉醒后")]
    assert _paragraphs(blocks) == ["登场：迎接我的狂风吧", "登场：风，听我号令"]

    # 没说名字在哪个属性上：内容照常保留，只是不带名字
    plain = _blocks(VOICES, CleanSettings(tabber_item_class="tabbertab"))
    assert _headings(plain) == [(2, "式神语音")]
    assert _paragraphs(plain) == ["登场：迎接我的狂风吧", "登场：风，听我号令"]


def test_tabs_holding_only_images_leave_the_list_of_their_names() -> None:
    blocks = _blocks(
        """
        <h2>式神简介</h2>
        <div class="tabber">
          <div class="tabbertab" title="默认"><a class="image"><img alt="a.png" src="a"></a></div>
          <div class="tabbertab" title="闲问兰音"><div><img alt="闲问兰音" src="b.png"></div></div>
          <div class="tabbertab" title="明烛天南">
            <a class="new" title="文件:明烛天南.png">文件:明烛天南.png</a>
          </div>
        </div>
        <p>此世即我世，如月满无缺。</p>
        """
    )
    # 图片的 alt 和标签页的名字一样的不重复留；没有文字的标签页不立小标题，
    # 后面的正文还在原来的章节里
    assert _headings(blocks) == [(2, "式神简介")]
    assert _paragraphs(blocks) == ["默认、闲问兰音、明烛天南", "此世即我世，如月满无缺。"]


def test_tabs_without_the_attribute_are_still_paired_with_buttons() -> None:
    blocks = _blocks(
        """
        <div class="tabber">
          <div class="tabbertab">第一段</div><div class="tabbertab">第二段</div>
          <ul>
            <li class="tabber-filter-item">传记一</li><li class="tabber-filter-item">传记二</li>
          </ul>
        </div>
        """,
        CleanSettings(
            remove_classes=frozenset({"tabber-filter-item"}),
            tabber_item_class="tabbertab",
            tabber_label_attribute="title",
        ),
    )
    assert _headings(blocks) == [(1, "传记一"), (1, "传记二")]
    assert _paragraphs(blocks) == ["第一段", "第二段"]


def _chapter(lines: int) -> str:
    """折叠框里的一章剧情：表头「全文」，下面一格是整章的对白，一句一个 div。"""
    dialogue = "".join(
        f'<div class="line"><span class="name">大天狗</span><span>：</span>'
        f"<span>这是第{i}句台词，说的是赛道上的事。</span></div>"
        for i in range(lines)
    )
    return (
        "<h2>第一章 彷徨的羽翼</h2>"
        '<table class="mw-collapsible"><tbody>'
        "<tr><th>全文</th></tr>"
        f"<tr><td><div>夜色浓墨一般。</div>{dialogue}</td></tr>"
        "</tbody></table>"
    )


def test_a_single_column_table_is_a_frame_not_a_table() -> None:
    blocks, stats = _convert(_chapter(2), CleanSettings())
    # 表头成了内容前面的一行，格子里的内容照普通内容处理：一句一段
    assert [b.kind for b in blocks] == [BLOCK_HEADING, *[BLOCK_PARA] * 4]
    assert _paragraphs(blocks) == [
        "全文",
        "夜色浓墨一般。",
        "大天狗：这是第0句台词，说的是赛道上的事。",
        "大天狗：这是第1句台词，说的是赛道上的事。",
    ]
    assert (stats.total, stats.single_column, stats.markdown_tables) == (1, 1, 0)

    # 没有表头的也一样；格子里的列表还是列表
    blocks = _blocks(
        "<table><tr><td>第一段</td></tr><tr><td><ul><li>甲</li><li>乙</li></ul></td></tr></table>"
    )
    assert [b.render() for b in blocks] == ["第一段", "- 甲\n- 乙"]

    # 有两列的还是表格（哪怕只有一行）
    blocks = _blocks(
        "<table><tr><th>所属式神</th><th>皮肤名</th></tr><tr><td>雪女</td><td>京都</td></tr></table>"
    )
    assert [b.kind for b in blocks] == [BLOCK_TABLE]


def test_a_chapter_inside_a_frame_is_split_into_chunks() -> None:
    blocks = _blocks(_chapter(120))
    doc = Document(
        doc_key="page:1", kind=KIND_ENTRY, title="飞羽凌云", url="https://w/x", source="wiki 条目",
        page_id=1, rev_id=2, blocks=blocks,
    )  # fmt: skip
    cfg = ChunkSettings(target_chars=400, max_chars=600, min_chars=60)
    chunks = chunk_document(doc, cfg, ChunkStats())
    # 以前整章是一行表格，多长都是一片；现在按段落切开，每片都带着章节的标题路径
    assert len(chunks) > 5
    assert all(len(c.body) <= cfg.max_chars for c in chunks)
    assert {c.header for c in chunks} == {"飞羽凌云 > 第一章 彷徨的羽翼"}
    said = [line for c in chunks for line in c.body.split("\n") if line.startswith("大天狗：")]
    assert said == [f"大天狗：这是第{i}句台词，说的是赛道上的事。" for i in range(120)]  # 不丢不重
