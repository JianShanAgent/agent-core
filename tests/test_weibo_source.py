"""纯离线微博标准资料校验与出处。"""

from pathlib import Path

import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.core.sources import SourceSetup, source_instance
from jianshanagent.platforms.weibo import create_source_adapter
from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document
from jianshanagent.plugins.knowledge.settings import ChunkSettings


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        tmp_path / "config",
        {
            "core.wiki": {"wikis": []},
            "core.sources": {
                "sources": [
                    {"id": "social", "label": "测试讨论", "platform": "weibo", "role": "community"},
                ]
            },
        },
        {},
    )


def normalize(tmp_path, rows):
    conf = config(tmp_path)
    return create_source_adapter(SourceSetup(conf, source_instance(conf, "social"))).normalize(rows)


def row(**kwargs):
    return {"version": 1, "id": "12345", "text": "这是一条本地测试帖子。", **kwargs}


def test_post_identity_date_and_role(tmp_path):
    doc = normalize(
        tmp_path,
        [
            row(
                published_at="2026-05-03T23:30:00+08:00",
                author_id="42",
                author_name="本地作者",
                url="https://weibo.example/42/12345",
            )
        ],
    )[0]
    assert doc.kind == "post"
    assert doc.doc_key == "weibo:12345"
    assert doc.date == "2026-05-03"
    assert doc.meta["canonical_document_id"] == "weibo:12345"
    assert doc.meta["source_role"] == "community"
    assert doc.meta["post_id"] == "12345"
    assert "origin_id" not in doc.meta
    assert doc.meta["author_id"] == "42"
    assert doc.page_id is None
    chunks = chunk_document(doc, ChunkSettings(), ChunkStats())
    assert chunks[0].meta == doc.meta
    assert "社区讨论" in chunks[0].header


def test_no_fabricated_date(tmp_path):
    doc = normalize(tmp_path, [row(text="活动日期 2026-12-10，不是本帖发布日期")])[0]
    assert doc.date is None
    assert doc.meta["published_at"] is None
    assert doc.date_inferred is None


@pytest.mark.parametrize(
    "extra",
    [
        {"wiki": "other"},
        {"source_id": "other"},
        {"chunk_id": "evil"},
        {"fingerprint": "evil"},
        {"path": "../../tmp"},
        {"meta": {"source_role": "official"}},
        {"source_role": "official"},
        {"attachments": ["https://example.org/private"]},
        {"version": True},
        {"id": "../evil"},
        {"id": "123ABC"},
        {"id": "１２３"},
        {"id": "1" * 33},
        {"id": 123},
        {"published_at": "2026-05-03"},
        {"published_at": "2026-05-03T10:00:00"},
        {"url": "file:///tmp/foo"},
        {"url": "javascript:alert(1)"},
        {"url": "https://user:password@example.org/"},
        {"url": "https://example.org\\@evil/"},
        {"url": "https://example.org/\nprivate"},
        {"url": "https://example.org:wrong/"},
        {"text": ""},
        {"text": "a" * 100_001},
        {"repost": {"id": "12345", "text": "self"}},
    ],
)
def test_reject_untrusted_fields_and_urls(tmp_path, extra):
    with pytest.raises(ValueError):
        normalize(tmp_path, [row(**extra)])


def test_duplicate_ids_rejected_even_identical(tmp_path):
    with pytest.raises(ValueError, match="重复或冲突"):
        normalize(tmp_path, [row(), row()])


def test_structured_blocks_and_repost(tmp_path):
    data = row()
    data.pop("text")
    data["blocks"] = [
        {"kind": "heading", "text": "评论", "level": 1},
        {"kind": "para", "text": "我自己的评论"},
        {"kind": "list", "items": ["第一条", "第二条"]},
    ]
    data["repost"] = {"id": "67890", "text": "被转发原文", "author_name": "原作者"}
    doc = normalize(tmp_path, [data])[0]
    assert doc.doc_key == "weibo:12345"
    assert doc.meta["repost_of"] == "weibo:67890"
    assert doc.meta["origin_id"] == "67890"
    assert doc.meta["post_id"] == "12345"
    assert doc.meta["canonical_document_id"] == "weibo:12345"
    assert "我自己的评论" in doc.blocks[1].text
    assert doc.blocks[-1].text == "被转发原文"


def test_empty_batch(tmp_path):
    assert normalize(tmp_path, []) == []
