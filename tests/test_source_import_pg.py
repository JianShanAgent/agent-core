"""Real isolated PG coverage for source import publication and recovery boundaries."""

from __future__ import annotations

import json
import os
from array import array
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import asyncpg
import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.core.db import MIGRATIONS_DIR, Database
from jianshanagent.core.embedding import EmbeddingSettings
from jianshanagent.core.sources import disabled_source_ids
from jianshanagent.plugins.knowledge import source_import
from jianshanagent.plugins.knowledge.embed import VectorStore, read_embeddable
from jianshanagent.plugins.knowledge.news import NewsQuery
from jianshanagent.plugins.knowledge.source_filter import SourceFilter
from jianshanagent.plugins.knowledge.source_import import SourceImportError, SourceImportService
from jianshanagent.plugins.knowledge.store import KbChunk, PostgresKnowledgeStore

DIM = 4096
UNIT = [1.0] + [0.0] * (DIM - 1)


@pytest.fixture
async def pg_source_store() -> AsyncIterator[PostgresKnowledgeStore]:
    dsn = os.environ.get("TEST_DSN")
    if not dsn or urlsplit(dsn).hostname not in ("127.0.0.1", "localhost", "::1"):
        pytest.skip("requires an explicit isolated local TEST_DSN")
    schema = "test_source_import_" + uuid4().hex
    base, _, params = dsn.partition("?")
    settings = dict(parse_qsl(params, keep_blank_values=True))
    settings["search_path"] = schema + ",public"
    db = Database(base + "?" + urlencode(settings))
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await db.connect()
        await db.pool.execute((MIGRATIONS_DIR / "0003_knowledge.sql").read_text())
        await db.pool.execute("ALTER TABLE kb_chunks ADD COLUMN wiki TEXT NOT NULL DEFAULT ''")
        yield PostgresKnowledgeStore(db)
    finally:
        await db.close()
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        tmp_path / "config",
        {
            "core.wiki": {
                "wikis": [
                    {"id": "huiji", "platform": "huiji", "api_url": "https://wiki.test/api.php"}
                ]
            },
            "core.sources": {
                "sources": [
                    {"id": "source_a", "label": "甲微博", "platform": "weibo", "role": "official"},
                    {"id": "source_b", "label": "乙微博", "platform": "weibo", "role": "community"},
                ]
            },
            "plugin.knowledge": {"paths": {"root": str(tmp_path / "kb")}},
            "core.embedding": {
                "enabled": True,
                "default_provider": "test",
                "providers": {
                    "test": {
                        "base_url": "http://must-not-connect.invalid",
                        "model": "fixture",
                        "dimensions": DIM,
                        "text_instruction": "fixed fixture instruction",
                    }
                },
            },
        },
        {},
    )


async def local_vectors(provider, chunks: Path, directory: Path) -> float:
    assert provider.dimensions == DIM and provider.text_instruction == "fixed fixture instruction"
    vectors = VectorStore(directory, provider)
    vectors.open()
    try:
        for item in read_embeddable(chunks):
            if not vectors.has(item.chunk_id, item.digest):
                vectors.append([(item.chunk_id, item.digest, array("f", UNIT))])
    finally:
        vectors.close()
    return 1.0


def post(identity: str = "101", text: str = "近期维护公告", day: str = "2026-11-24") -> str:
    return json.dumps(
        {
            "version": 1,
            "id": identity,
            "text": text,
            "published_at": day + "T08:00:00+08:00",
            "author_id": "5896401674",
            "url": "https://weibo.com/5896401674/fixture",
        },
        ensure_ascii=False,
    )


async def test_real_pg_two_sources_wiki_dedup_and_disable(
    tmp_path: Path, pg_source_store: PostgresKnowledgeStore
) -> None:
    cfg = config(tmp_path)
    provider = EmbeddingSettings.from_config(cfg).provider()
    wiki = KbChunk.from_json(
        {
            "chunk_id": "mirror",
            "doc_key": "weibo:101",
            "type": "announcement",
            "title": "Wiki 收录维护公告",
            "text": "近期维护公告",
            "date": "2026-11-24",
            "meta": {"source_platform": "weibo"},
        },
        wiki="huiji",
    )
    await pg_source_store.sync([(wiki, UNIT)], fingerprint=provider.fingerprint, wiki="huiji")
    original_hash = wiki.row_hash(provider.fingerprint)
    service = SourceImportService(cfg, pg_source_store, local_vectors)
    try:
        assert service.preview("source_a", post())["new"] == 1
        result = await service.import_data("source_a", post())
        assert result["state"] == "completed"
        assert (await service.import_data("source_a", post()))["noop"]
        await service.import_data("source_b", post("202", "玩家社区观察", "2026-11-25"))
        recent = await pg_source_store.recent_documents(NewsQuery(("weibo",), as_of="2026-11-26"))
        assert recent.matching_documents == 2 and len(recent.chunks) == 2
        filtered = await pg_source_store.recent_documents(
            NewsQuery(
                None, as_of="2026-11-26", source_filter=SourceFilter(source_ids=("source_a",))
            )
        )
        assert filtered.matching_documents == 1 and filtered.chunks[0].wiki == "source_a"
        cfg.files["core.sources"]["sources"][1]["mounted"] = False
        hidden = await pg_source_store.recent_documents(
            NewsQuery(("weibo",), as_of="2026-11-26"), exclude_wikis=disabled_source_ids(cfg)
        )
        assert hidden.matching_documents == 1
        assert set((await pg_source_store.info()).wikis) == {"huiji", "source_a", "source_b"}
        stored = await pg_source_store.get([wiki.chunk_id])
        assert stored[0].row_hash(provider.fingerprint) == original_hash
    finally:
        await service.aclose()


async def test_real_pg_pointer_failure_restores_the_old_source(
    tmp_path: Path, pg_source_store: PostgresKnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    service = SourceImportService(cfg, pg_source_store, local_vectors)
    try:
        await service.import_data("source_a", post())
        before = await pg_source_store.info()
        old_current = service._current(service._directory("source_a"))
        atomic = source_import._atomic_json

        def fail_pointer(path: Path, value) -> None:
            if path.name == "current.json":
                raise OSError("simulated pointer publication failure")
            atomic(path, value)

        monkeypatch.setattr(source_import, "_atomic_json", fail_pointer)
        with pytest.raises(SourceImportError):
            await service.import_data("source_a", post("303", "不得半批提交"))
        after = await pg_source_store.info()
        assert after.chunks == before.chunks
        assert service._current(service._directory("source_a")) == old_current
        result = await pg_source_store.recent_documents(NewsQuery(("weibo",), as_of="2026-11-26"))
        assert result.matching_documents == 1 and result.chunks[0].doc_key == "source_a:weibo:101"
    finally:
        await service.aclose()


async def test_real_pg_pending_journal_recovers_before_an_empty_retry(
    tmp_path: Path, pg_source_store: PostgresKnowledgeStore
) -> None:
    cfg = config(tmp_path)
    provider = EmbeddingSettings.from_config(cfg).provider()
    service = SourceImportService(cfg, pg_source_store, local_vectors)
    try:
        await service.import_data("source_a", post())
        directory = service._directory("source_a")
        current = service._current(directory)
        snapshot = service._snapshot(directory, current)
        pending = KbChunk.from_json(
            {
                "chunk_id": "pending",
                "doc_key": "weibo:404",
                "type": "post",
                "title": "未发布",
                "text": "模拟事务完成但current未发布",
            },
            wiki="source_a",
        )
        await pg_source_store.sync(
            [*service._records(snapshot, "source_a"), (pending, UNIT)],
            fingerprint=provider.fingerprint,
            wiki="source_a",
        )
        (directory / "journal.json").write_text(
            json.dumps(
                {"fingerprint": provider.fingerprint, "previous": current, "candidate": "f" * 32}
            )
        )
        assert (await pg_source_store.info()).chunks == 2
        retry = await service.import_data("source_a", "")
        assert retry["noop"] and retry["state"] == "completed"
        assert (await pg_source_store.info()).chunks == 1
        assert not (directory / "journal.json").exists()
        assert await pg_source_store.get([pending.chunk_id]) == []
    finally:
        await service.aclose()
