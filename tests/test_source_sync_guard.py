"""Wiki housekeeping must preserve independently registered document sources."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.plugins.knowledge import sync
from jianshanagent.plugins.knowledge.store import KbChunk, MemoryKnowledgeStore


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        tmp_path / "config",
        {
            "core.wiki": {
                "wikis": [
                    {"id": "huiji", "platform": "huiji", "api_url": "https://wiki.test/api.php"},
                    {"id": "bwiki", "platform": "bwiki", "api_url": "https://bwiki.test/api.php"},
                ],
                "wikis_disabled": ["bwiki"],
            },
            "core.sources": {
                "sources": [
                    {"id": "weibo_a", "label": "甲微博", "platform": "weibo"},
                    {"id": "weibo_b", "label": "乙微博", "platform": "weibo", "mounted": False},
                ]
            },
        },
        {},
    )


async def test_wiki_cleanup_keeps_active_and_disabled_document_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryKnowledgeStore()
    for source_id in ("huiji", "bwiki", "weibo_a", "weibo_b", "obsolete"):
        chunk = KbChunk.from_json(
            {
                "chunk_id": "part",
                "doc_key": "document",
                "type": "entry",
                "title": "资料",
                "text": "正文",
            },
            wiki=source_id,
        )
        await store.sync([(chunk, None)], fingerprint="test", wiki=source_id)
    db = SimpleNamespace(close=AsyncMock())
    images = SimpleNamespace(keep_only=AsyncMock(return_value=0))
    monkeypatch.setattr(sync, "_connect", AsyncMock(return_value=db))
    monkeypatch.setattr(sync, "PostgresKnowledgeStore", lambda connection: store)
    monkeypatch.setattr(sync, "PostgresImageStore", lambda connection: images)

    assert await sync.run_wikis(config(tmp_path), [], forget_others=True) == {}
    assert set((await store.info()).wikis) == {"huiji", "bwiki", "weibo_a", "weibo_b"}
    images.keep_only.assert_awaited_once_with(["huiji", "bwiki"])
    db.close.assert_awaited_once()


async def test_bad_source_registration_cannot_become_an_empty_cleanup_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = config(tmp_path)
    broken.override_errors["core.sources"] = "invalid YAML"
    db = SimpleNamespace(close=AsyncMock())
    chunks = SimpleNamespace(keep_only=AsyncMock(return_value=0))
    images = SimpleNamespace(keep_only=AsyncMock(return_value=0))
    monkeypatch.setattr(sync, "_connect", AsyncMock(return_value=db))
    monkeypatch.setattr(sync, "PostgresKnowledgeStore", lambda connection: chunks)
    monkeypatch.setattr(sync, "PostgresImageStore", lambda connection: images)
    with pytest.raises(ValueError, match="来源登记覆写无效"):
        await sync.run_wikis(broken, [], forget_others=True)
    chunks.keep_only.assert_not_awaited()
    images.keep_only.assert_not_awaited()
    db.close.assert_awaited_once()
