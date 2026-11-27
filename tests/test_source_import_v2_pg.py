"""Real PostgreSQL + 4096-dimensional stream-v2 publication boundaries."""

import json

import pytest

from jianshanagent.plugins.knowledge import source_import_v2 as v2
from jianshanagent.plugins.knowledge.source_import import SourceImportError, SourceImportService
from jianshanagent.plugins.knowledge.source_import_v2 import get_source_row
from test_source_import import batch
from test_source_import_pg import pg_source_store as pg_source_store
from test_source_import_v2 import current, setup


async def service_for_pg(tmp_path, store):
    service, _, embed = setup(tmp_path, dimensions=4096)
    service.store = store

    async def probe(members):
        measured = await store.resource_usage(members)
        # Test-only trusted probe; this container fixture is not deployment evidence.
        return {**measured, "pg_disk_free_bytes": 2**60}

    service.resource_probe = probe
    return service, embed


async def test_real_pg_v2_incremental_binary_reuse(tmp_path, pg_source_store):
    service, embed = await service_for_pg(tmp_path, pg_source_store)
    await service.import_data("social", batch() + "\n" + batch("101"))
    pointer = current(service)
    first = service._directory("social") / "generations" / pointer["generation"]
    vector = first / "vectors" / "vectors.f32"
    original = vector.read_bytes()
    result = await service.import_data("social", batch("102"))
    assert result["total_documents"] == 3 and result["sync"]["total"] == 1
    assert embed.new == 3 and vector.read_bytes() == original
    noop = await service.import_data("social", batch())
    assert noop["noop"] and embed.new == 3
    assert (await pg_source_store.info()).wikis["social"].chunks == 3
    dimensions = await pg_source_store._db.pool.fetchval(
        "SELECT vector_dims(embedding::vector) FROM kb_chunks WHERE wiki='social' LIMIT 1"
    )
    assert dimensions == 4096


async def test_real_pg_v2_pointer_failure_restores_document_scope(
    tmp_path, pg_source_store, monkeypatch
):
    service, embed = await service_for_pg(tmp_path, pg_source_store)
    await service.import_data("social", batch() + "\n" + batch("101"))
    before = current(service)
    atomic = v2._atomic_json

    def fail(path, value):
        if path.name == "current.json":
            raise OSError("test pointer unavailable")
        return atomic(path, value)

    monkeypatch.setattr(v2, "_atomic_json", fail)
    with pytest.raises(SourceImportError):
        await service.import_data("social", batch(text="替换失败") + "\n" + batch("102"))
    assert current(service) == before
    assert (await pg_source_store.info()).wikis["social"].chunks == 2
    keys = await pg_source_store._db.pool.fetch(
        "SELECT doc_key,text FROM kb_chunks WHERE wiki='social'"
    )
    assert {row["doc_key"] for row in keys} == {"social:weibo:100", "social:weibo:101"}
    assert all("替换失败" not in row["text"] for row in keys)
    assert get_source_row(service.config, "social", "102") is None


async def test_real_pg_v1_51_migration_preserves_vectors_and_zero_embeddings(
    tmp_path, pg_source_store
):
    service, embed = await service_for_pg(tmp_path, pg_source_store)
    del service.config.files["core.sources"]["sources"][0]["storage"]
    content = "\n".join(batch(str(100 + i)) for i in range(51))
    await service.import_data("social", content)
    before = await pg_source_store._db.pool.fetch(
        "SELECT chunk_id,embedding::text AS vector FROM kb_chunks "
        "WHERE wiki='social' ORDER BY chunk_id"
    )
    assert embed.new == 51
    service.config.files["core.sources"]["sources"][0]["storage"] = "stream_v2"
    restarted = SourceImportService(
        service.config,
        pg_source_store,
        embed,
        v2_limits=service.v2_limits,
        resource_probe=service.resource_probe,
    )
    assert (await restarted.import_data("social", batch()))["noop"]
    after = await pg_source_store._db.pool.fetch(
        "SELECT chunk_id,embedding::text AS vector FROM kb_chunks "
        "WHERE wiki='social' ORDER BY chunk_id"
    )
    assert before == after and embed.new == 51
    pointer = json.loads((service._directory("social") / "current.json").read_text())
    assert pointer["version"] == 2
