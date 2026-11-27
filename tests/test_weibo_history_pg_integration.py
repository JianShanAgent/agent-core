"""Real PG publication through V2 service; vectors/disk probe are explicit synthetic fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.plugins.knowledge.source_filter import SourceFilter
from jianshanagent.plugins.knowledge.source_import import SourceImportService
from jianshanagent.plugins.knowledge.source_import_v2 import get_source_row
from jianshanagent.plugins.knowledge.store import PostgresKnowledgeStore
from jianshanagent.plugins.knowledge.terms import extract_terms
from test_source_import_pg import config, local_vectors, post
from test_source_import_pg import pg_source_store as pg_source_store

UID = "5896401674"


def configured(tmp_path: Path) -> AppConfig:
    cfg = config(tmp_path)
    root, child = cfg.files["core.sources"]["sources"]
    root.update(
        storage="stream_v2",
        collector={
            "uid": UID,
            "mode": "full_then_incremental",
            "include_comments": True,
            "include_replies": True,
            "discussion_source_id": "source_b",
        },
    )
    child.update(storage="stream_v2", discussion_of="source_a")
    return cfg


def discussion(text: str = "官方账号在评论区回答") -> str:
    return json.dumps(
        {
            "version": 2,
            "id": "101:201",
            "post_id": "101",
            "post_author_id": UID,
            "post_url": "https://weibo.com/5896401674/fixture",
            "nodes": [
                {
                    "id": "201",
                    "root_id": "201",
                    "parent_id": None,
                    "reply_to_id": None,
                    "author_id": "1234",
                    "author_name": "合成玩家",
                    "published_at": "2026-11-24T09:00:00+08:00",
                    "text": "玩家在评论区提问",
                },
                {
                    "id": "202",
                    "root_id": "201",
                    "parent_id": "201",
                    "reply_to_id": "201",
                    "author_id": UID,
                    "author_name": "合成官方",
                    "published_at": "2026-11-24T10:00:00+08:00",
                    "text": text,
                },
            ],
        },
        ensure_ascii=False,
    )


def service_for(cfg, store):
    async def synthetic_disk_probe(source_ids):
        # DB sizes/WAL/start are real. Filesystem allowance is deliberately fixture-only.
        return {**await store.resource_usage(source_ids), "pg_disk_free_bytes": 20 * 1024**3}

    return SourceImportService(cfg, store, local_vectors, resource_probe=synthetic_disk_probe)


async def digest(store, source):
    return await store._db.pool.fetchval(
        "SELECT md5(string_agg(chunk_id||row_hash,'|' ORDER BY chunk_id)) "
        "FROM kb_chunks WHERE wiki=$1",
        source,
    )


async def test_real_pg_v2_posts_discussion_scope_noop_and_actual_authors(
    tmp_path: Path,
    pg_source_store: PostgresKnowledgeStore,
):
    cfg = configured(tmp_path)
    svc = service_for(cfg, pg_source_store)
    try:
        await svc.import_data("source_a", post("101") + "\n" + post("102", "另一篇官方微博"))
        root_before = await digest(pg_source_store, "source_a")
        result = await svc.import_data("source_b", discussion())
        assert result["state"] == "completed"
        row = get_source_row(cfg, "source_b", "101:201")
        assert row["nodes"][1]["author_id"] == UID
        chunks = await pg_source_store._db.pool.fetch(
            "SELECT meta,type FROM kb_chunks WHERE wiki='source_b'"
        )
        assert chunks and all(r["type"] == "discussion" for r in chunks)
        metas = [json.loads(r["meta"]) if isinstance(r["meta"], str) else r["meta"] for r in chunks]
        assert all(m["source_role"] == "community" for m in metas)
        assert any(UID in m["author_ids"] for m in metas)
        pointer = tmp_path / "kb" / "source_b" / "source-import" / "current.json"
        original_pointer = hashlib.sha256(pointer.read_bytes()).hexdigest()
        assert (await svc.import_data("source_b", discussion()))["noop"]
        assert hashlib.sha256(pointer.read_bytes()).hexdigest() == original_pointer
        await svc.import_data("source_b", discussion("官方账号修改了这条楼中楼回复"))
        assert await digest(pg_source_store, "source_a") == root_before
        count = await pg_source_store._db.pool.fetchval(
            "SELECT count(DISTINCT doc_key) FROM kb_chunks WHERE wiki='source_a'"
        )
        assert count == 2
        only_target = SourceFilter(
            source_ids=("source_b",),
            author_ids=(UID,),
            content_types=("discussion",),
            target_reply_only=True,
        )
        hits = await pg_source_store.keyword_search(
            extract_terms("楼中楼"), limit=20, max_df_ratio=1.0, source_filter=only_target
        )
        assert hits and all(hit.chunk.wiki == "source_b" for hit in hits)
        exclude = SourceFilter(exclude_content_types=("discussion",))
        assert not await pg_source_store.keyword_search(
            extract_terms("楼中楼"), limit=20, max_df_ratio=1.0, source_filter=exclude
        )
    finally:
        await svc.aclose()


async def test_real_pg_v2_pointer_failure_restores_only_affected_discussion(
    tmp_path: Path,
    pg_source_store: PostgresKnowledgeStore,
    monkeypatch,
):
    from jianshanagent.plugins.knowledge import source_import_v2 as v2

    cfg = configured(tmp_path)
    svc = service_for(cfg, pg_source_store)
    try:
        await svc.import_data("source_a", post())
        await svc.import_data("source_b", discussion())
        root_before = await digest(pg_source_store, "source_a")
        discussion_before = await digest(pg_source_store, "source_b")
        atomic = v2._atomic_json
        failed = False

        def fail_once(path, value):
            nonlocal failed
            if path.name == "current.json" and path.parent.parent.name == "source_b" and not failed:
                failed = True
                raise OSError("synthetic pointer failure after real PG commit")
            return atomic(path, value)

        monkeypatch.setattr(v2, "_atomic_json", fail_once)
        with pytest.raises((ValueError, OSError)):
            await svc.import_data("source_b", discussion("不应留下的合成变更"))
        assert failed
        assert await digest(pg_source_store, "source_a") == root_before
        assert await digest(pg_source_store, "source_b") == discussion_before
        assert (
            get_source_row(cfg, "source_b", "101:201")["nodes"][1]["text"] == "官方账号在评论区回答"
        )
    finally:
        await svc.aclose()
