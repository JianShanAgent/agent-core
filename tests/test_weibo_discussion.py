"""合成讨论数据：父帖归属、独立身份、完整长文与成员出处。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from jianshanagent.core.sources import SourceSetup, source_instance
from jianshanagent.platforms.weibo import WeiboSourceAdapter
from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document
from jianshanagent.plugins.knowledge.settings import ChunkSettings
from jianshanagent.plugins.knowledge.source_filter import (
    SourceFilter,
    canonical_document_id,
    canonical_sql,
    provenance,
)
from jianshanagent.plugins.knowledge.store import KbChunk
from test_weibo_source import config


def node(node_id="101", **extra):
    return {
        "id": node_id,
        "root_id": "101",
        "parent_id": None,
        "reply_to_id": None,
        "author_id": "7",
        "author_name": "测试用户",
        "published_at": "2026-05-03T10:00:00+08:00",
        "text": "合成玩家反馈。",
        **extra,
    }


def packet(**extra):
    return {
        "version": 2,
        "id": "12345:101",
        "post_id": "12345",
        "post_author_id": "42",
        "post_url": "https://weibo.com/42/abc",
        "nodes": [node()],
        **extra,
    }


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    conf = config(tmp_path)
    conf.files["core.sources"]["sources"] = [
        {"id": "parent", "platform": "weibo", "role": "official", "collector": {"uid": "42"}},
    ]
    parent = source_instance(conf, "parent")
    child = replace(parent, id="discussion", role="community", raw={"discussion_of": "parent"})
    monkeypatch.setattr(
        "jianshanagent.plugins.knowledge.source_import_v2.get_source_row",
        lambda *_: {
            "version": 1,
            "id": "12345",
            "author_id": "42",
            "url": "https://weibo.com/42/abc",
        },
    )
    return WeiboSourceAdapter(SourceSetup(conf, child))


def as_kb(chunk):
    return KbChunk.from_json(chunk.to_json(), wiki="discussion")


def test_identity_actual_authors_and_exact_uid(adapter):
    doc = adapter.normalize(
        [
            packet(
                nodes=[
                    node(author_name="官方账号"),
                    node(
                        "102",
                        parent_id="101",
                        reply_to_id="101",
                        author_id="42",
                        author_name="普通昵称",
                    ),
                ]
            )
        ]
    )[0]
    assert doc.kind == "discussion"
    assert doc.doc_key == "weibo:discussion:12345:101"
    assert doc.meta["canonical_document_id"] != "weibo:12345"
    assert doc.meta["source_role"] == "community"
    assert doc.meta["author_ids"] == ["7", "42"]
    assert "author_id" not in doc.meta
    assert doc.meta["target_reply_ids"] == ["102"]
    chunks = chunk_document(doc, ChunkSettings(), ChunkStats())
    assert len(chunks) == 1  # 短评论打包，而不是每节点一个向量。
    kb = as_kb(chunks[0])
    assert canonical_document_id(kb) == doc.doc_key
    details = provenance(kb)
    assert details["evidence_group"] == "weibo:12345"
    assert details["discussion_nodes"][1]["url"].endswith("#comment-102")
    assert "社区讨论" in chunks[0].header


def test_complete_long_node_reconstructs_and_chunk_members_are_local(adapter):
    original = "  长评论\n" + "甲乙。\n" * 1200 + "末尾  "
    doc = adapter.normalize(
        [
            packet(
                nodes=[
                    node(text=original),
                    node("102", root_id="102", author_id="42", text="官方合成回复"),
                ]
            )
        ]
    )[0]
    chunks = chunk_document(doc, ChunkSettings(), ChunkStats())
    assert len(chunks) > 2
    fragments = []
    for chunk in chunks:
        assert len(chunk.body) <= 1200
        assert sum(len(n["text"]) for n in chunk.meta["discussion_nodes"]) <= 1200
        for member in chunk.meta["discussion_nodes"]:
            assert chunk.body[member["body_start"] : member["body_end"]] == member["text"]
            if member["id"] == "101":
                fragments.append(member)
        assert SourceFilter(author_ids=("42",)).allows(as_kb(chunk)) == (
            "42" in chunk.meta["author_ids"]
        )
        assert SourceFilter(target_reply_only=True).allows(as_kb(chunk)) == bool(
            chunk.meta["target_reply_ids"]
        )
    assert "".join(n["text"] for n in sorted(fragments, key=lambda n: n["text_start"])) == original
    assert fragments[0]["text_start"] == 0
    assert fragments[-1]["text_end"] == len(original)
    assert all(n["text_length"] == len(original) for n in fragments)
    assert doc.meta["discussion_nodes"][0]["text"] == original


@pytest.mark.parametrize(
    "changes",
    [
        {"post_author_id": "7"},
        {"post_id": "0"},
        {"id": "12345:999"},
        {"source_role": "official"},
        {"post_url": "https://evil.test/42/abc"},
        {"post_url": "http://weibo.com/42/abc"},
        {"post_url": "https://weibo.com/7/abc"},
        {"post_url": "https://weibo.com/42/other"},
        {"post_url": "https://weibo.com/42/abc?x=1"},
        {"nodes": []},
        {"nodes": [node(str(i), root_id=str(i)) for i in range(65)]},
        {"nodes": [node(text="甲" * 100001)]},
        {"nodes": [node(), node("102", root_id="102", text="甲" * 100000)]},
        {"nodes": [node(published_at=None)]},
        {"nodes": [node(published_at="2026-01-01")]},
        {"nodes": [node(author_id="not-id")]},
        {"nodes": [node(root_id="abc")]},
        {"nodes": [node(parent_id="101")]},
        {"nodes": [node(reply_to_id="101")]},
        {"nodes": [node(author_name="a" * 201)]},
        {"nodes": [node(text="\x00")]},
        {"nodes": [node(), node()]},
        {"nodes": [node(meta={"official": True})]},
        {"nodes": [node(media_refs=["https://127.0.0.1/private"])]},
        {"nodes": [node(media_refs=["https://user:pw@media.test/a"])]},
        {"nodes": [node(media_refs=["https://media.test/a"] * 17)]},
        {"nodes": [node(), node("102", root_id="102", parent_id="101")]},
        {
            "nodes": [
                node(root_id="999", parent_id="102"),
                node("102", root_id="999", parent_id="101"),
            ]
        },
    ],
)
def test_reject_invalid_packets(adapter, changes):
    with pytest.raises(ValueError):
        adapter.normalize([packet(**changes)])


def test_parent_ownership_does_not_trust_packet_assertion(adapter, monkeypatch):
    monkeypatch.setattr(
        "jianshanagent.plugins.knowledge.source_import_v2.get_source_row",
        lambda *_: {"version": 1, "id": "12345", "author_id": "7"},
    )
    with pytest.raises(ValueError, match="已发布"):
        adapter.normalize([packet()])


def test_only_linked_community_accepts_v2_and_rejects_v1(adapter):
    with pytest.raises(ValueError, match="version 2"):
        adapter.normalize([{"version": 1, "id": "12345", "text": "正文"}])
    for child in (
        replace(adapter.setup.source, role="official"),
        replace(adapter.setup.source, raw={}),
    ):
        other = WeiboSourceAdapter(SourceSetup(adapter.setup.config, child))
        with pytest.raises(ValueError, match="community"):
            other.normalize([packet()])


def test_missing_node_relation_is_unknown_not_invented(adapter):
    data = packet(nodes=[node(parent_id="999", root_id="999", reply_to_id=None)])
    doc = adapter.normalize([data])[0]
    assert doc.meta["discussion_nodes"][0]["parent_id"] == "999"
    assert doc.meta["discussion_nodes"][0]["reply_to_id"] is None
    del data["nodes"][0]["parent_id"]
    with pytest.raises(ValueError, match="必要字段"):
        adapter.normalize([data])


def test_duplicate_node_under_different_packet_rejected(adapter):
    second = deepcopy(packet())
    second["id"] = "12345:102"
    second["nodes"].append(node("102"))
    with pytest.raises(ValueError, match="多个包"):
        adapter.normalize([packet(), second])


def test_filter_membership_content_type_and_sql(adapter):
    doc = adapter.normalize([packet()])[0]
    kb = as_kb(chunk_document(doc, ChunkSettings(), ChunkStats())[0])
    assert SourceFilter(content_types=("discussion",), author_ids=("7",)).allows(kb)
    assert not SourceFilter(exclude_content_types=("discussion",)).allows(kb)
    assert not SourceFilter(exclude_author_ids=("7",)).allows(kb)
    assert SourceFilter(discussion_of_ids=("parent",)).allows(kb)
    assert not SourceFilter(discussion_of_ids=()).allows(kb)
    assert not SourceFilter(target_reply_only=True).allows(kb)
    selected = SourceFilter(
        content_types=("discussion",),
        author_ids=("7",),
        exclude_author_ids=("42",),
        discussion_of_ids=("parent",),
        target_reply_only=True,
    )
    sql, args = selected.sql("c", 3)
    assert "c.type = ANY($3::text[])" in sql
    assert "author_ids" in sql and "?|" in sql
    assert "target_reply_ids" in sql
    assert args == [["discussion"], ["parent"], ["7"], ["42"]]
    assert selected.intersect(SourceFilter()).target_reply_only
    assert selected.with_legacy_source("parent").target_reply_only
    assert "weibo:discussion:" in canonical_sql("c")
    assert (
        canonical_document_id(
            replace(
                kb,
                meta={
                    "source_platform": "weibo",
                    "canonical_document_id": "weibo:12345",
                },
            )
        )
        is None
    )


def test_legacy_packet_defaults_logical_identity(adapter):
    doc = adapter.normalize([packet()])[0]
    assert doc.meta["packet_id"] == doc.meta["logical_packet_id"] == "12345:101"
    assert doc.meta["fragment_id"] == "12345:101"


def test_edit_growth_and_shrink_keep_logical_packet_and_all_nodes(adapter):
    initial_nodes = [node(str(i), text="短" * 1000) for i in range(101, 133)]
    before = adapter.normalize([packet(nodes=initial_nodes)])[0]
    assert before.text_chars() == 32000
    edited_nodes = deepcopy(initial_nodes)
    edited_nodes[0]["text"] = "长" * 80000
    # 同逻辑包111000字不能通过提高上限或截断处理，物理分片各自仍<=100000字。
    with pytest.raises(ValueError, match="总正文"):
        adapter.normalize([packet(packet_id="12345:101", nodes=edited_nodes)])
    fragments = [
        packet(packet_id="12345:101", nodes=edited_nodes[:21]),
        packet(id="12345:122", packet_id="12345:101", nodes=edited_nodes[21:]),
    ]
    after = adapter.normalize(fragments)
    assert [doc.doc_key for doc in after] == [
        "weibo:discussion:12345:101",
        "weibo:discussion:12345:122",
    ]
    assert all(doc.meta["logical_packet_id"] == "12345:101" for doc in after)
    assert all(doc.text_chars() <= 100000 for doc in after)
    assert sum(doc.text_chars() for doc in after) == 111000
    expected = {entry["id"]: entry["text"] for entry in edited_nodes}
    observed = {
        entry["id"]: entry["text"] for doc in after for entry in doc.meta["discussion_nodes"]
    }
    assert observed == expected
    assert sum(len(doc.meta["node_ids"]) for doc in after) == len(expected)
    # 重启/重放同组物理片段不会改变身份或文本；逻辑seed不必出现在后续物理片段。
    assert adapter.normalize(deepcopy(fragments)) == after
    for doc in after:
        for chunk in chunk_document(doc, ChunkSettings(), ChunkStats()):
            details = provenance(as_kb(chunk))
            assert details["packet_id"] == details["logical_packet_id"] == "12345:101"
            assert details["fragment_id"] == doc.meta["fragment_id"]
    shrunk = adapter.normalize([packet(packet_id="12345:101", nodes=initial_nodes)])[0]
    assert shrunk.meta["logical_packet_id"] == before.meta["logical_packet_id"]
    assert shrunk.doc_key == before.doc_key
    assert {entry["id"] for entry in shrunk.meta["discussion_nodes"]} == set(expected)


@pytest.mark.parametrize("packet_id", [None, 123, True, "12345", "0:101", "12345:abc", "12345:1:2"])
def test_reject_invalid_logical_packet_identity(adapter, packet_id):
    with pytest.raises(ValueError, match="packet_id"):
        adapter.normalize([packet(packet_id=packet_id)])


def test_explicit_fragment_identity_uses_its_first_native_node(adapter):
    with pytest.raises(ValueError, match="第一个原生节点"):
        adapter.normalize([packet(packet_id="12345:101", nodes=[node("102"), node()])])


def test_physical_fragments_must_not_duplicate_nodes(adapter):
    rows = [
        packet(packet_id="12345:101"),
        packet(id="12345:102", packet_id="12345:101", nodes=[node("102"), node()]),
    ]
    with pytest.raises(ValueError, match="多个包"):
        adapter.normalize(rows)


def test_reject_cross_fragment_relation_cycle(adapter):
    rows = [
        packet(packet_id="12345:101", nodes=[node(root_id="999", parent_id="102")]),
        packet(
            id="12345:102",
            packet_id="12345:101",
            nodes=[
                node("102", root_id="999", parent_id="101"),
            ],
        ),
    ]
    with pytest.raises(ValueError, match="成环"):
        adapter.normalize(rows)


def test_logical_packet_node_bound_applies_across_all_fragments(adapter):
    rows = [
        packet(packet_id="12345:101", nodes=[node(str(i)) for i in range(101, 165)]),
        packet(id="12345:165", packet_id="12345:101", nodes=[node("165")]),
    ]
    with pytest.raises(ValueError, match="合计不得超过 64"):
        adapter.normalize(rows)


def test_full_packet_text_is_bounded_without_truncation(adapter):
    doc = adapter.normalize([packet(nodes=[node(text="甲" * 100000)])])[0]
    assert doc.text_chars() == 100000
    assert (
        sum(
            len(member["text"])
            for chunk in chunk_document(doc, ChunkSettings(), ChunkStats())
            for member in chunk.meta["discussion_nodes"]
        )
        == 100000
    )
