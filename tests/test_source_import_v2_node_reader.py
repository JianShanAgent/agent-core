"""Indexed, archive-authentic discussion root lookups for target-self validation."""

import json

import pytest

from jianshanagent.plugins.knowledge import source_import_v2 as v2
from jianshanagent.plugins.knowledge.source_import import SourceImportError
from jianshanagent.plugins.knowledge.source_import_v2 import get_source_node
from test_source_import import batch
from test_source_import_v2 import discussion_config, packet, setup
from test_source_import_v2_fragments import payload, rows_for_edit


async def discussion_service(tmp_path):
    service, store, embed = setup(tmp_path)
    discussion_config(service)
    await service.import_data("social", batch(author_id="42"))
    return service, store, embed


async def test_node_lookup_keeps_real_author_relations_and_published_post(tmp_path):
    service, _, _ = await discussion_service(tmp_path)
    row = json.loads(packet(node_ids=["200", "201"]))
    row["nodes"][1].update(root_id="200", parent_id="200", reply_to_id="200")
    await service.import_data("comments", json.dumps(row))
    root = get_source_node(service.config, "comments", "200")
    assert root["id"] == root["root_id"] == "200"
    assert root["author_id"] == "42" and root["post_id"] == "100"
    reply = get_source_node(service.config, "comments", "201")
    assert reply["id"] == "201" and reply["root_id"] == "200"
    assert reply["parent_id"] == "200" and reply["reply_to_id"] == "200"
    # A target account reply cannot fabricate the root's actual original author.
    assert reply["author_id"] == "99" and reply["post_id"] == "100"
    assert get_source_node(service.config, "comments", "999") is None


async def test_node_lookup_uses_index_and_only_the_matching_logical_group(tmp_path, monkeypatch):
    service, _, _ = await discussion_service(tmp_path)
    await service.import_data("comments", packet(seed="300", node_ids=["300"]))
    await service.import_data("comments", payload(rows_for_edit(expanded=True)))
    read = v2._raw
    visited = []

    def bounded(entry):
        assert entry["id"] in {"100:200", "100:221"}
        visited.append(entry["id"])
        return read(entry)

    def forbidden(*args, **kwargs):
        pytest.fail("a native-node read must not scan all rows or legacy snapshots")

    monkeypatch.setattr(v2, "_raw", bounded)
    monkeypatch.setattr(v2, "iter_source_rows", forbidden)
    monkeypatch.setattr(v2, "_entries", forbidden)
    monkeypatch.setattr(v2, "stream_v1_snapshot", forbidden)
    node = get_source_node(service.config, "comments", "231")
    assert node["id"] == node["root_id"] == "231" and node["post_id"] == "100"
    assert node["author_id"] == "99"
    assert 1 <= len(visited) <= 2
    visited.clear()
    assert get_source_node(service.config, "comments", "999") is None
    assert visited == []


async def test_node_lookup_resolves_new_fragment_and_never_reads_tombstoned_original(
    tmp_path, monkeypatch
):
    service, _, _ = await discussion_service(tmp_path)
    await service.import_data("comments", payload(rows_for_edit(expanded=True)))
    assert len(get_source_node(service.config, "comments", "200")["text"]) == 80000
    collapsed = rows_for_edit()
    collapsed[0]["nodes"][21]["text"] = "合成编辑后的当前节点"
    await service.import_data("comments", payload(collapsed))
    original = v2._raw

    def reject_obsolete(entry):
        assert entry["id"] != "100:221", "obsolete physical fragments must stay shadowed"
        return original(entry)

    monkeypatch.setattr(v2, "_raw", reject_obsolete)
    node = get_source_node(service.config, "comments", "221")
    assert node["text"] == "合成编辑后的当前节点" and node["post_id"] == "100"
    assert len(get_source_node(service.config, "comments", "200")["text"]) == 1000


async def test_v1_non_discussion_and_missing_sources_do_not_load_snapshot(tmp_path, monkeypatch):
    service, _, _ = setup(tmp_path)
    del service.config.files["core.sources"]["sources"][0]["storage"]
    await service.import_data("social", batch())

    def forbidden(*args, **kwargs):
        pytest.fail("V1 posts have no persisted discussion nodes")

    monkeypatch.setattr(v2, "stream_v1_snapshot", forbidden)
    assert get_source_node(service.config, "social", "100") is None
    # Invalid node identities do not reach any archive path.
    assert get_source_node(service.config, "social", "../100") is None
    assert get_source_node(service.config, "social", "") is None
    assert get_source_node(service.config, "social", "1" * 33) is None


@pytest.mark.parametrize("forgery", ["foreign_author", "different_post", "not_root"])
async def test_target_self_adapter_rejects_forged_persisted_root(tmp_path, forgery):
    service, store, _ = await discussion_service(tmp_path)
    original = json.loads(packet(node_ids=["200", "201"]))
    original["nodes"][0]["author_id"] = "99" if forgery == "foreign_author" else "42"
    original["nodes"][1].update(author_id="42", root_id="200", parent_id="200", reply_to_id="200")
    await service.import_data("comments", json.dumps(original))
    post_id = "101" if forgery == "different_post" else "100"
    if post_id == "101":
        await service.import_data("social", batch("101", author_id="42"))
    # Direct test-fixture scope change only; deployment migration has its own zero-data guard.
    service.config.files["core.sources"]["sources"][0]["collector"]["discussion_scope"] = (
        "target_self"
    )
    incoming = json.loads(packet(seed="202"))
    incoming.update(
        id=post_id + ":202", post_id=post_id, post_url="https://weibo.com/detail/" + post_id
    )
    root = "201" if forgery == "not_root" else "200"
    incoming["nodes"][0].update(author_id="42", root_id=root, parent_id=root, reply_to_id=root)
    before = dict(store._chunks)
    with pytest.raises(SourceImportError, match="根评论"):
        await service.import_data("comments", json.dumps(incoming))
    assert store._chunks == before
    assert get_source_node(service.config, "comments", "202") is None
