"""Stream-v2 publication tests, entirely synthetic and offline."""

import asyncio
import json
import sqlite3
from array import array
from dataclasses import replace

import pytest

from jianshanagent.plugins.knowledge import source_import_v2 as v2
from jianshanagent.plugins.knowledge.embed import VectorStore, read_embeddable
from jianshanagent.plugins.knowledge.source_import import (
    SourceImportCapacityError,
    SourceImportError,
    SourceImportService,
)
from jianshanagent.plugins.knowledge.source_import_v2 import (
    StreamImportLimits,
    get_source_row,
    iter_source_rows,
)
from jianshanagent.plugins.knowledge.store import MemoryKnowledgeStore
from test_source_import import batch, conf, docs


async def resource_probe(members):
    return {
        "database_bytes": 100,
        "wal_position_bytes": 100,
        "pg_started_at": 1234567890000000,
        "pg_disk_free_bytes": 2**60,
    }


class Embed:
    def __init__(self):
        self.new = 0
        self.calls = 0
        self.minimum = 1.0
        self.started = asyncio.Event()
        self.release = None

    async def __call__(self, provider, chunks, directory):
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        vectors = VectorStore(directory, provider)
        vectors.open()
        try:
            vector = array("f", [1] + [0] * (provider.dimensions - 1))
            for item in read_embeddable(chunks):
                vectors.append([(item.chunk_id, item.digest, vector)])
                self.new += 1
        finally:
            vectors.close()
        return self.minimum


def setup(tmp_path, *, dimensions=2, config_provider=None):
    config = conf(tmp_path)
    config.files["core.sources"]["sources"][0]["storage"] = "stream_v2"
    config.files["core.embedding"]["providers"]["test"]["dimensions"] = dimensions
    store = MemoryKnowledgeStore()
    embedder = Embed()
    service = SourceImportService(
        config,
        store,
        embedder,
        v2_limits=StreamImportLimits(disk_safety_bytes=1),
        config_provider=config_provider,
        resource_probe=resource_probe,
    )
    return service, store, embedder


def current(service):
    directory = service._directory("social")
    return json.loads((directory / "current.json").read_text())


async def test_incremental_immutable_references_and_gc(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    report = await service.import_data("social", batch())
    assert report["total_documents"] == 1
    assert report["storage"] == "stream_v2"
    first = current(service)["generation"]
    directory = service._directory("social")
    vector = directory / "generations" / first / "vectors" / "vectors.f32"
    original = vector.read_bytes()
    first_manifest = directory / "generations" / first / "manifest.sqlite"
    immutable = first_manifest.read_bytes()
    assert not (first_manifest.parent / "snapshot.json").exists()
    noop = await service.import_data("social", batch())
    assert noop["noop"] and embed.new == 1 and current(service)["generation"] == first
    await service.import_data("social", batch("101"))
    await service.import_data("social", batch("102"))
    assert vector.read_bytes() == original and first_manifest.read_bytes() == immutable
    assert get_source_row(service.config, "social", "100")["text"] == "本地知识测试正文"
    assert len(list(iter_source_rows(service.config, "social"))) == 3
    assert len(docs(store)) == 3 and embed.new == 3
    # Existing readers must not invoke the mutating VectorStore.open at all.
    monkeypatch.setattr(VectorStore, "open", lambda self: pytest.fail("old shard opened mutable"))
    assert service.preview("social", batch())["unchanged"] == 1
    assert (await service.import_data("social", batch()))["noop"]
    assert vector.read_bytes() == original


async def test_updates_replace_only_affected_doc_and_keep_other_namespace(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch() + "\n" + batch("101"))
    other = SourceImportService(service.config, store, embed)
    await other.import_data("other", batch("999"))
    before = {k: v for k, v in store._chunks.items() if v.wiki == "other"}
    changed = await service.import_data("social", batch(text="编辑后的测试正文"))
    assert changed["updated"] == 1 and changed["total_documents"] == 2
    assert embed.new == 4
    assert docs(store) == {"social:weibo:100", "social:weibo:101"}
    assert before == {k: v for k, v in store._chunks.items() if v.wiki == "other"}
    assert get_source_row(service.config, "social", "100")["text"] == "编辑后的测试正文"


async def test_v1_migration_reuses_all_51_vectors_without_snapshot_load(tmp_path, monkeypatch):
    config = conf(tmp_path)
    store, embed = MemoryKnowledgeStore(), Embed()
    old = SourceImportService(config, store, embed)
    content = "\n".join(batch(str(100 + n)) for n in range(51))
    await old.import_data("social", content)
    old_pointer = json.loads((old._directory("social") / "current.json").read_text())
    assert embed.new == 51
    config.files["core.sources"]["sources"][0]["storage"] = "stream_v2"
    service = SourceImportService(
        config,
        store,
        embed,
        v2_limits=StreamImportLimits(disk_safety_bytes=1),
        resource_probe=resource_probe,
    )
    assert get_source_row(config, "social", "150")["id"] == "150"
    assert service.preview("social", batch())["total_documents"] == 51
    monkeypatch.setattr(service, "_snapshot", lambda *a: pytest.fail("full float snapshot load"))
    result = await service.import_data("social", batch())
    assert result["noop"] and embed.new == 51
    assert current(service)["version"] == 2
    assert current(service)["previous"] == old_pointer["generation"]
    await service.import_data("social", batch("200"))
    assert embed.new == 52 and len(docs(store)) == 52
    assert len(list(iter_source_rows(config, "social"))) == 52
    assert (service._directory("social") / "generations" / old_pointer["generation"]).exists()


@pytest.mark.parametrize("minimum", [0.989, float("nan"), float("inf")])
async def test_verification_failure_never_publishes(tmp_path, minimum):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    before = current(service)
    embed.minimum = minimum
    with pytest.raises(SourceImportError):
        await service.import_data("social", batch("101"))
    assert current(service) == before and len(docs(store)) == 1
    assert get_source_row(service.config, "social", "101") is None


async def test_pointer_failure_after_pg_restores_scoped_docs(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch() + "\n" + batch("101"))
    before = current(service)
    original = v2._atomic_json
    failed = False

    def pointer_failure(path, value):
        nonlocal failed
        if path.name == "current.json" and not failed:
            failed = True
            raise OSError("synthetic pointer replacement failure")
        return original(path, value)

    monkeypatch.setattr(v2, "_atomic_json", pointer_failure)
    with pytest.raises(SourceImportError):
        await service.import_data("social", batch(text="改动") + "\n" + batch("102"))
    assert current(service) == before
    assert docs(store) == {"social:weibo:100", "social:weibo:101"}
    assert not (service._directory("social") / "journal.json").exists()
    assert all("改动" not in c.text for c in store._chunks.values())
    assert get_source_row(service.config, "social", "102") is None


async def test_pending_journal_restart_restores_only_scope(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    before = current(service)
    atomic, sync = v2._atomic_json, store.sync_documents
    attempts = 0

    async def fail_recovery(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            raise RuntimeError("synthetic rollback unavailable")
        return await sync(*args, **kwargs)

    def fail_pointer(path, value):
        if path.name == "current.json":
            raise OSError("synthetic pointer failure")
        return atomic(path, value)

    monkeypatch.setattr(store, "sync_documents", fail_recovery)
    monkeypatch.setattr(v2, "_atomic_json", fail_pointer)
    with pytest.raises(SourceImportError, match="恢复"):
        await service.import_data("social", batch("101"))
    assert service.status("social")["state"] == "recovery_required"
    assert len(docs(store)) == 2
    monkeypatch.setattr(v2, "_atomic_json", atomic)
    monkeypatch.setattr(store, "sync_documents", sync)
    restarted = SourceImportService(
        service.config, store, embed, v2_limits=service.v2_limits, resource_probe=resource_probe
    )
    assert (await restarted.import_data("social", batch()))["noop"]
    assert current(service) == before and len(docs(store)) == 1


@pytest.mark.parametrize(
    "field,code",
    [
        ("max_documents", "documents"),
        ("max_chunks", "chunks"),
        ("max_vector_bytes", "vector_bytes"),
        ("max_archive_bytes", "snapshot_bytes"),
        ("max_metadata_bytes", "metadata_bytes"),
        ("max_disk_bytes", "snapshot_bytes"),
        ("max_working_bytes", "working_bytes"),
        ("disk_safety_bytes", "disk_free_bytes"),
    ],
)
async def test_capacity_block_preserves_old_data(tmp_path, field, code):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    before = current(service)
    service.v2_limits = replace(
        service.v2_limits, **{field: 1 if field != "disk_safety_bytes" else 2**60}
    )
    with pytest.raises(SourceImportCapacityError) as exc:
        await service.import_data("social", batch("101"))
    assert exc.value.code == code
    assert current(service) == before and len(docs(store)) == 1 and embed.new == 1


async def test_cancellation_and_config_reload_before_publication(tmp_path):
    service, store, embed = setup(tmp_path)
    embed.release = asyncio.Event()
    task = asyncio.create_task(service.import_data("social", batch()))
    await embed.started.wait()
    assert service.cancel("social")
    embed.release.set()
    assert (await task)["state"] == "cancelled"
    assert not docs(store) and get_source_row(service.config, "social", "100") is None
    embed.release = None
    embed.started.clear()
    await service.import_data("social", batch())
    before = current(service)
    embed.started.clear()
    embed.release = asyncio.Event()
    task = asyncio.create_task(service.import_data("social", batch("101")))
    await embed.started.wait()
    service.config.files["core.sources"]["sources"][0]["label"] = "新配置"
    embed.release.set()
    with pytest.raises(SourceImportError, match="配置"):
        await task
    assert current(service) == before and len(docs(store)) == 1


async def test_v2_pointer_forces_v2_even_if_config_marker_removed(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    del service.config.files["core.sources"]["sources"][0]["storage"]
    await service.import_data("social", batch("101"))
    assert current(service)["version"] == 2 and len(docs(store)) == 2


async def test_4096_dimensions_bounded_batches(tmp_path):
    service, store, embed = setup(tmp_path, dimensions=4096)
    await service.import_data("social", "\n".join(batch(str(100 + n)) for n in range(200)))
    result = await service.import_data("social", batch("999"))
    assert result["vector_bytes"] == 201 * 4096 * 4 and embed.new == 201
    assert len(docs(store)) == 201
    # Each new immutable manifest stores just its delta, not 201 copied rows/vectors.
    stage = service._directory("social") / "generations" / current(service)["generation"]
    with sqlite3.connect(stage / "manifest.sqlite") as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert (stage / "vectors" / "vectors.f32").stat().st_size == 4096 * 4


async def test_more_than_7000_documents_over_finite_batches(tmp_path):
    service, store, embed = setup(tmp_path)
    for first in range(0, 7001, 200):
        end = min(first + 200, 7001)
        report = await service.import_data(
            "social", "\n".join(batch(str(10000 + n)) for n in range(first, end))
        )
    assert report["total_documents"] == 7001 and report["chunks"] == 7001
    assert len(docs(store)) == embed.new == 7001
    assert sum(1 for _ in iter_source_rows(service.config, "social")) == 7001
    assert get_source_row(service.config, "social", "10000")["id"] == "10000"


def packet(seed="200", node_ids=None):
    ids = node_ids or [seed]
    return json.dumps(
        {
            "version": 2,
            "id": "100:" + seed,
            "post_id": "100",
            "post_author_id": "42",
            "post_url": "https://weibo.com/detail/100",
            "nodes": [
                {
                    "id": node_id,
                    "root_id": node_id,
                    "parent_id": None,
                    "reply_to_id": None,
                    "author_id": "42" if i == 0 else "99",
                    "author_name": "合成公开昵称",
                    "published_at": "2026-01-01T00:00:00Z",
                    "text": "合成测试评论文本",
                }
                for i, node_id in enumerate(ids)
            ],
        },
        ensure_ascii=False,
    )


def discussion_config(service):
    raw = service.config.files["core.sources"]["sources"]
    raw[0]["collector"] = {"uid": "42"}
    raw[0]["role"] = "official"
    raw.append(
        {
            "id": "comments",
            "platform": "weibo",
            "role": "community",
            "storage": "stream_v2",
            "discussion_of": "social",
        }
    )


async def test_discussion_requires_persisted_parent_and_keeps_actual_authors(tmp_path):
    service, store, embed = setup(tmp_path)
    discussion_config(service)
    with pytest.raises(SourceImportError):
        await service.import_data("comments", packet())
    await service.import_data("social", batch(author_id="42"))
    result = await service.import_data(
        "comments", packet(node_ids=[str(200 + i) for i in range(32)])
    )
    assert result["total_documents"] == 1 and result["chunks"] < 32
    raw = get_source_row(service.config, "comments", "100:200")
    assert len(raw["nodes"]) == 32 and raw["nodes"][1]["author_id"] == "99"
    chunks = [chunk for chunk in store._chunks.values() if chunk.wiki == "comments"]
    assert all(
        chunk.kind == "discussion" and chunk.meta["source_role"] == "community" for chunk in chunks
    )
    assert all(chunk.doc_key == "comments:weibo:discussion:100:200" for chunk in chunks)
    unchanged = embed.new
    assert (
        await service.import_data("comments", packet(node_ids=[str(200 + i) for i in range(32)]))
    )["noop"]
    assert embed.new == unchanged
    with pytest.raises(SourceImportError, match="稳定讨论包"):
        await service.import_data("comments", packet(seed="201", node_ids=["201", "200"]))
    # A packet's assertion cannot turn an existing foreign parent into the bound UID.
    service.config.files["core.sources"]["sources"][0]["collector"]["uid"] = "99"
    forged = json.loads(packet())
    forged["post_author_id"] = "99"
    with pytest.raises(SourceImportError):
        await service.import_data("comments", json.dumps(forged))


async def test_commit_is_shielded_from_repeated_task_cancellation(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    sync = store.sync_documents

    async def held(*args, **kwargs):
        started.set()
        await release.wait()
        return await sync(*args, **kwargs)

    monkeypatch.setattr(store, "sync_documents", held)
    task = asyncio.create_task(service.import_data("social", batch()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert service.active("social") and not service.cancel("social")
    release.set()
    with pytest.raises(SourceImportError) as exc:
        await task
    assert getattr(exc.value, "published", False)
    assert current(service)["version"] == 2 and len(docs(store)) == 1
    assert not (service._directory("social") / "journal.json").exists()


async def test_shared_pg_growth_budget_applies_to_direct_import(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    before = current(service)

    async def grown(members):
        usage = await resource_probe(members)
        usage["database_bytes"] += 3 * 1024**3
        usage["wal_position_bytes"] += 2 * 1024**3
        return usage

    service.resource_probe = grown
    with pytest.raises(SourceImportCapacityError) as exc:
        await service.import_data("social", batch("101"))
    assert exc.value.code == "snapshot_bytes"
    assert current(service) == before and len(docs(store)) == 1


async def test_default_probe_requires_owner_only_same_volume_proof(tmp_path, monkeypatch):
    import os

    service, store, embed = setup(tmp_path)
    service.resource_probe = None

    async def measured(members):
        values = await resource_probe(members)
        values.pop("pg_disk_free_bytes")
        return values

    monkeypatch.setattr(store, "resource_usage", measured)
    with pytest.raises(SourceImportCapacityError) as exc:
        await service.import_data("social", batch())
    assert exc.value.code == "resource_measurement" and embed.new == 0
    root = service._directory("social").parent.parent
    proof = root / ".source-storage-volume.json"
    device = root.stat().st_dev
    value = {"kb_device": device, "pg_device": device, "pg_started_at": 1234567890000000}
    proof.write_text(json.dumps(value))
    os.chmod(proof, 0o600)
    await service.import_data("social", batch())
    assert len(docs(store)) == 1
    for field, wrong in (
        ("pg_device", device + 1),
        ("pg_started_at", 0),
        ("pg_started_at", "1234567890000000"),
    ):
        proof.write_text(json.dumps({**value, field: wrong}))
        with pytest.raises(SourceImportCapacityError) as exc:
            await service.import_data("social", batch("101"))
        assert exc.value.code == "resource_measurement"
    proof.write_text(json.dumps(value))
    os.chmod(proof, 0o644)
    with pytest.raises(SourceImportCapacityError):
        await service.import_data("social", batch("101"))
    assert len(docs(store)) == 1


async def test_generation_and_namespace_directories_fsynced_before_pg(tmp_path, monkeypatch):
    import os

    service, store, embed = setup(tmp_path)
    synced = set()
    fsync = os.fsync

    def observed(fd):
        stat = os.fstat(fd)
        synced.add((stat.st_dev, stat.st_ino))
        fsync(fd)

    monkeypatch.setattr(os, "fsync", observed)
    original = store.sync_documents

    async def guarded(*args, **kwargs):
        directory = service._directory("social")
        stage = next(path for path in (directory / "generations").iterdir())
        for path in (
            stage / "vectors",
            stage,
            stage.parent,
            directory,
            directory.parent,
            directory.parent.parent,
        ):
            stat = path.stat()
            assert (stat.st_dev, stat.st_ino) in synced, str(path)
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "sync_documents", guarded)
    await service.import_data("social", batch())
