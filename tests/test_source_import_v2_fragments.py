"""Logical discussions survive edits that repartition immutable physical fragments."""

import json

import pytest

from jianshanagent.plugins.knowledge import source_import_v2 as v2
from jianshanagent.plugins.knowledge.source_import import SourceImportError, SourceImportService
from jianshanagent.plugins.knowledge.source_import_v2 import get_source_row, iter_source_rows
from test_source_import import batch
from test_source_import_pg import pg_source_store as pg_source_store
from test_source_import_v2 import discussion_config, packet, setup
from test_source_import_v2_pg import service_for_pg


def rows_for_edit(*, expanded=False):
    raw = json.loads(packet(node_ids=[str(200 + n) for n in range(32)]))
    for node in raw["nodes"]:
        node["text"] = "原" * 1000
    raw["packet_id"] = "100:200"
    if not expanded:
        return [raw]
    raw["nodes"][0]["text"] = "长" * 80000
    first = {**raw, "nodes": raw["nodes"][:21]}
    second = {**raw, "id": "100:221", "nodes": raw["nodes"][21:]}
    assert sum(len(node["text"]) for node in first["nodes"]) == 100000
    assert sum(len(node["text"]) for node in second["nodes"]) == 11000
    return [first, second]


def payload(rows):
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)


def archived(service):
    rows = list(iter_source_rows(service.config, "comments"))
    nodes = [node for row in rows for node in row["nodes"]]
    assert len(nodes) == len({node["id"] for node in nodes})
    return {node["id"]: node["text"] for node in nodes}


async def exercise_expansion_and_shrink(service, embed):
    discussion_config(service)
    await service.import_data("social", batch(author_id="42"))
    await service.import_data("comments", packet(seed="300", node_ids=["300"]))
    initial = rows_for_edit()
    # Legacy no-packet_id rows remain a single logical group.
    initial[0].pop("packet_id")
    await service.import_data("comments", payload(initial))
    before_nodes = archived(service)
    assert len(before_nodes) == 33
    expanded = rows_for_edit(expanded=True)
    result = await service.import_data("comments", payload(expanded))
    assert result["total_documents"] == 3 and result["new"] == 1
    assert len(archived(service)) == 33 and len(archived(service)["200"]) == 80000
    count = embed.new
    restarted = SourceImportService(
        service.config,
        service.store,
        embed,
        v2_limits=service.v2_limits,
        resource_probe=service.resource_probe,
    )
    replay = await restarted.import_data("comments", payload(expanded))
    assert replay["noop"] and embed.new == count
    assert len(archived(restarted)) == 33
    # A physical-only partial import must not quietly drop the other fragment's nodes.
    with pytest.raises(SourceImportError, match="全部片段"):
        await restarted.import_data("comments", payload(expanded[:1]))
    collapsed = await restarted.import_data("comments", payload(rows_for_edit()))
    assert collapsed["deleted"] == 1 and collapsed["total_documents"] == 2
    assert get_source_row(service.config, "comments", "100:221") is None
    assert archived(restarted) == before_nodes
    assert (await restarted.import_data("comments", payload(rows_for_edit())))["noop"]
    return restarted


async def test_edit_expansion_replay_restart_and_tombstone_scope(tmp_path):
    service, store, embed = setup(tmp_path)
    restarted = await exercise_expansion_and_shrink(service, embed)
    assert {chunk.doc_key for chunk in store._chunks.values() if chunk.wiki == "comments"} == {
        "comments:weibo:discussion:100:200",
        "comments:weibo:discussion:100:300",
    }
    assert get_source_row(restarted.config, "comments", "100:300") is not None


async def test_fragment_scope_pointer_failure_restores_all_old_physical_docs(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    discussion_config(service)
    await service.import_data("social", batch(author_id="42"))
    await service.import_data("comments", payload(rows_for_edit(expanded=True)))
    old_nodes = archived(service)
    original = v2._atomic_json

    def fail_pointer(path, value):
        if path.name == "current.json":
            raise OSError("synthetic failure after scoped fragment replacement")
        return original(path, value)

    monkeypatch.setattr(v2, "_atomic_json", fail_pointer)
    with pytest.raises(SourceImportError):
        await service.import_data("comments", payload(rows_for_edit()))
    assert archived(service) == old_nodes
    assert get_source_row(service.config, "comments", "100:221") is not None
    assert {chunk.doc_key for chunk in store._chunks.values() if chunk.wiki == "comments"} == {
        "comments:weibo:discussion:100:200",
        "comments:weibo:discussion:100:221",
    }
    monkeypatch.setattr(v2, "_atomic_json", original)
    await service.import_data("comments", payload(rows_for_edit()))
    assert get_source_row(service.config, "comments", "100:221") is None


async def test_real_pg_fragment_replacement_preserves_other_logical_groups(
    tmp_path, pg_source_store
):
    service, embed = await service_for_pg(tmp_path, pg_source_store)
    restarted = await exercise_expansion_and_shrink(service, embed)
    keys = await pg_source_store._db.pool.fetch(
        "SELECT DISTINCT doc_key FROM kb_chunks WHERE wiki='comments'"
    )
    assert {row["doc_key"] for row in keys} == {
        "comments:weibo:discussion:100:200",
        "comments:weibo:discussion:100:300",
    }
    assert len(archived(restarted)) == 33


async def test_real_pg_fragment_journal_restart_restores_deleted_scope(
    tmp_path, pg_source_store, monkeypatch
):
    service, embed = await service_for_pg(tmp_path, pg_source_store)
    discussion_config(service)
    await service.import_data("social", batch(author_id="42"))
    await service.import_data("comments", packet(seed="300", node_ids=["300"]))
    await service.import_data("comments", payload(rows_for_edit(expanded=True)))
    before_nodes = archived(service)
    atomic, sync = v2._atomic_json, pg_source_store.sync_documents
    calls = 0

    async def fail_restore(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("synthetic recovery unavailable")
        return await sync(*args, **kwargs)

    def fail_pointer(path, value):
        if path.name == "current.json":
            raise OSError("synthetic pointer failure")
        return atomic(path, value)

    monkeypatch.setattr(pg_source_store, "sync_documents", fail_restore)
    monkeypatch.setattr(v2, "_atomic_json", fail_pointer)
    with pytest.raises(SourceImportError, match="恢复"):
        await service.import_data("comments", payload(rows_for_edit()))
    assert service.status("comments")["state"] == "recovery_required"
    keys = await pg_source_store._db.pool.fetch(
        "SELECT DISTINCT doc_key FROM kb_chunks WHERE wiki='comments'"
    )
    assert {row["doc_key"] for row in keys} == {
        "comments:weibo:discussion:100:200",
        "comments:weibo:discussion:100:300",
    }
    monkeypatch.setattr(pg_source_store, "sync_documents", sync)
    monkeypatch.setattr(v2, "_atomic_json", atomic)
    restarted = SourceImportService(
        service.config,
        pg_source_store,
        embed,
        v2_limits=service.v2_limits,
        resource_probe=service.resource_probe,
    )
    assert (await restarted.import_data("comments", payload(rows_for_edit(expanded=True))))["noop"]
    assert archived(restarted) == before_nodes
    keys = await pg_source_store._db.pool.fetch(
        "SELECT DISTINCT doc_key FROM kb_chunks WHERE wiki='comments'"
    )
    assert {row["doc_key"] for row in keys} == {
        "comments:weibo:discussion:100:200",
        "comments:weibo:discussion:100:221",
        "comments:weibo:discussion:100:300",
    }
    assert not (service._directory("comments") / "journal.json").exists()
    await restarted.import_data("comments", payload(rows_for_edit()))
    assert get_source_row(service.config, "comments", "100:221") is None


def pre_fragment_manifest(service, source_id):
    import sqlite3

    directory = service._directory(source_id)
    generation = json.loads((directory / "current.json").read_text())["generation"]
    path = directory / "generations" / generation / "manifest.sqlite"
    # Synthetic older-format fixture only: do not rewrite real immutable generations.
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE documents RENAME TO documents_new")
        db.execute(
            "CREATE TABLE documents(id TEXT PRIMARY KEY,doc_key TEXT UNIQUE NOT NULL,"
            "digest TEXT NOT NULL,raw_offset INTEGER NOT NULL,raw_length INTEGER NOT NULL,"
            "chunks TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO documents SELECT id,doc_key,digest,raw_offset,raw_length,chunks "
            "FROM documents_new"
        )
        db.execute("DROP TABLE documents_new")


async def test_pre_fragment_v2_root_and_single_packet_manifests_are_read_compatible(tmp_path):
    service, store, embed = setup(tmp_path)
    discussion_config(service)
    await service.import_data("social", batch(author_id="42"))
    pre_fragment_manifest(service, "social")
    assert get_source_row(service.config, "social", "100")["author_id"] == "42"
    await service.import_data("social", batch("101", author_id="42"))
    assert len(list(iter_source_rows(service.config, "social"))) == 2
    legacy = rows_for_edit()
    legacy[0].pop("packet_id")
    await service.import_data("comments", payload(legacy))
    pre_fragment_manifest(service, "comments")
    assert len(archived(service)) == 32
    await service.import_data("comments", payload(rows_for_edit(expanded=True)))
    assert len(archived(service)) == 32
    await service.import_data("comments", payload(rows_for_edit()))
    assert get_source_row(service.config, "comments", "100:221") is None
