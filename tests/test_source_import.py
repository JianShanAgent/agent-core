"""标准资料导入只用本地假嵌入和 MemoryKnowledgeStore。"""

import asyncio
import json
import sys
import threading
import uuid
from array import array
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.plugins.knowledge import cli, source_import
from jianshanagent.plugins.knowledge.embed import VectorStore, read_embeddable
from jianshanagent.plugins.knowledge.source_collect import (
    SourceCollectError,
    acquire_source_operation,
)
from jianshanagent.plugins.knowledge.source_import import (
    CAPACITY_ERROR_CODES,
    CAPACITY_PUBLIC_MESSAGE,
    ImportLimits,
    SourceImportCapacityError,
    SourceImportError,
    SourceImportPublishedError,
    SourceImportService,
)
from jianshanagent.plugins.knowledge.store import KbChunk, MemoryKnowledgeStore


def conf(tmp_path):
    return AppConfig(
        tmp_path / "config",
        {
            "core.wiki": {"wikis": []},
            "core.sources": {
                "sources": [
                    {"id": "social", "label": "本地微博", "platform": "weibo"},
                    {"id": "other", "label": "其它来源", "platform": "weibo"},
                ]
            },
            "plugin.knowledge": {"paths": {"root": str(tmp_path / "kb")}},
            "core.embedding": {
                "enabled": True,
                "default_provider": "test",
                "providers": {
                    "test": {
                        "base_url": "http://test.invalid",
                        "model": "fixed-model",
                        "dimensions": 2,
                        "text_instruction": "原有固定指令",
                    }
                },
            },
        },
        {},
    )


def batch(post_id="100", text="本地知识测试正文", **kwargs):
    return json.dumps({"version": 1, "id": post_id, "text": text, **kwargs}, ensure_ascii=False)


class FakeEmbedder:
    def __init__(self):
        self.calls = 0
        self.new = 0
        self.fail = False
        self.minimum = 1.0
        self.started = asyncio.Event()
        self.release = None

    async def __call__(self, provider, chunks, directory):
        assert provider.text_instruction == "原有固定指令"
        assert provider.model == "fixed-model"
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("api_key=should-never-leak")
        vectors = VectorStore(directory, provider)
        vectors.open()
        try:
            for item in read_embeddable(chunks):
                if not vectors.has(item.chunk_id, item.digest):
                    vectors.append([(item.chunk_id, item.digest, array("f", [1, 0]))])
                    self.new += 1
        finally:
            vectors.close()
        return self.minimum


def setup(tmp_path, limits=None, *, config_provider=None):
    store = MemoryKnowledgeStore()
    embedder = FakeEmbedder()
    service = SourceImportService(
        conf(tmp_path), store, embedder, limits=limits, config_provider=config_provider
    )
    return service, store, embedder


def docs(store, source="social"):
    return {c.doc_key for c in store._chunks.values() if c.wiki == source}


def test_preview_zero_embedding_and_no_files(tmp_path):
    service, store, embed = setup(tmp_path)
    preview = service.preview("social", batch())
    assert preview["new"] == preview["chunks"] == 1
    assert preview["sample"][0]["id"] == "100"
    assert service.status("social") is None
    assert not service.active("social")
    assert embed.calls == 0
    assert not (tmp_path / "kb").exists()
    assert not store._chunks


@pytest.mark.parametrize(
    "content",
    [
        b"\xff",
        '{"version":1,"id":"a","id":"b","text":"x"}',
        "[]",
        "{broken}",
        batch() + "\n" + batch(),
        batch() + '\n{"version":1,"id":"2","wiki":"evil"}',
    ],
)
def test_all_file_validation_precedes_writes(tmp_path, content):
    service, store, embed = setup(tmp_path)
    with pytest.raises(ValueError):
        service.preview("social", content)
    assert not store._chunks
    assert embed.calls == 0
    assert not (tmp_path / "kb").exists()


@pytest.mark.parametrize("author", [None, "999", " 42 "])
async def test_bound_source_rejects_missing_mixed_or_noncanonical_authors(tmp_path, author):
    service, store, embed = setup(tmp_path)
    service.config.files["core.sources"]["sources"][0]["collector"] = {
        "uid": "42",
        "enabled": False,  # Pausing the schedule does not remove the binding.
    }
    second = batch("101") if author is None else batch("101", author_id=author)
    content = f"{batch(author_id='42')}\n{second}"
    with pytest.raises(SourceImportError, match="第 2 条.*UID"):
        service.preview("social", content)
    with pytest.raises(SourceImportError, match="第 2 条.*UID"):
        await service.import_data("social", content)
    assert not (tmp_path / "kb").exists()
    assert not docs(store) and embed.calls == 0


async def test_bound_source_accepts_target_author_with_a_repost(tmp_path):
    service, store, _ = setup(tmp_path)
    service.config.files["core.sources"]["sources"][0]["collector"] = {"uid": "42"}
    content = batch(author_id="42", repost={"id": "50", "author_id": "999", "text": "转发正文"})
    assert service.preview("social", content)["new"] == 1
    assert (await service.import_data("social", content))["state"] == "completed"
    assert docs(store) == {"social:weibo:100"}


async def test_incremental_merge_idempotence_update_and_isolation(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("other", batch())
    first = await service.import_data("social", batch())
    assert first["state"] == "completed"
    assert embed.new == 2
    repeat = await service.import_data("social", batch())
    assert repeat["noop"]
    assert embed.calls == 2
    await service.import_data("social", batch("200", "新增正文"))
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}
    assert embed.new == 3  # 旧帖向量被复用
    await service.import_data("social", batch(text="同一原帖修改了正文"))
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}
    assert docs(store, "other") == {"other:weibo:100"}
    assert len(store._chunks) == 3
    assert all(chunk.kind == "post" for chunk in store._chunks.values())
    assert (await store.info()).with_vectors == 3
    assert (
        SourceImportService(service.config, store, embed).status("social")["state"] == "completed"
    )


async def test_empty_batch_never_clears_or_embeds(tmp_path):
    service, store, embed = setup(tmp_path)
    result = await service.import_data("social", "\n")
    assert result["noop"] and not store._chunks and embed.calls == 0
    await service.import_data("social", batch())
    before = dict(store._chunks)
    await service.import_data("social", "")
    assert store._chunks == before and embed.calls == 1


@pytest.mark.parametrize("content", [batch(), "\n"])
async def test_noop_requires_reachable_store_even_with_local_snapshot(
    tmp_path, monkeypatch, content
):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    saved = pointer(service)

    async def unavailable():
        raise ConnectionError("postgres connection includes private password")

    monkeypatch.setattr(store, "info", unavailable)
    with pytest.raises(SourceImportError) as error:
        await service.import_data("social", content)
    assert "private password" not in str(error.value)
    assert service.status("social")["state"] == "failed"
    assert pointer(service) == saved
    assert docs(store) == {"social:weibo:100"}
    assert embed.calls == 1


@pytest.mark.parametrize("failure", ["api", "verification"])
async def test_embedding_failure_preserves_previous(tmp_path, failure):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    old = dict(store._chunks)
    embed.fail = failure == "api"
    embed.minimum = 0.98 if failure == "verification" else 1.0
    with pytest.raises(SourceImportError) as error:
        await service.import_data("social", batch("200"))
    assert "should-never-leak" not in str(error.value)
    assert store._chunks == old
    assert service.status("social")["state"] == "failed"
    embed.fail, embed.minimum = False, 1.0
    await service.import_data("social", batch("200"))
    assert len(store._chunks) == 2


async def test_publication_failure_rolls_back_and_retry(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    old = dict(store._chunks)
    atomic = source_import._atomic_json

    def fail_pointer(path, value):
        if path.name == "current.json":
            raise OSError("simulated disk publication failure")
        atomic(path, value)

    monkeypatch.setattr(source_import, "_atomic_json", fail_pointer)
    with pytest.raises(SourceImportError):
        await service.import_data("social", batch("200"))
    assert store._chunks == old
    assert not (service._directory("social") / "journal.json").exists()
    monkeypatch.setattr(source_import, "_atomic_json", atomic)
    await service.import_data("social", batch("200"))
    assert len(store._chunks) == 2


async def test_cancel_and_cross_service_lock(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    embed.started.clear()
    embed.release = asyncio.Event()
    task = asyncio.create_task(service.import_data("social", batch("200")))
    await embed.started.wait()
    assert service.active("social")
    other = SourceImportService(service.config, store, embed)
    assert other.active("social")
    assert not other.cancel("social")
    assert other.status("social")["state"] == "embedding"
    with pytest.raises(SourceImportError, match="已有导入任务"):
        await service.import_data("social", batch("300"))
    with pytest.raises(SourceImportError, match="另一个"):
        await other.import_data("social", batch("300"))
    assert service.cancel("social")
    embed.release.set()
    result = await task
    assert result["state"] == "cancelled"
    assert docs(store) == {"social:weibo:100"}
    assert not service.active("social")


async def test_status_prefers_disk_and_recognizes_another_process(tmp_path):
    service, _, _ = setup(tmp_path)
    await service.import_data("social", batch())  # This service now has a stale cached job.
    directory = service._directory("social")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import fcntl, sys\n"
        "with open(sys.argv[1], 'ab') as lock:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.buffer.read(1)\n",
        str(directory / "import.lock"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), timeout=5) == b"locked\n"
        state = {"source_id": "social", "job_id": "external", "state": "embedding"}
        status_path = directory / "status.json"
        source_import._atomic_json(status_path, state)
        assert service.active("social")
        assert service.status("social") == state
        assert not service.cancel("social")

        state["state"] = "committing"
        source_import._atomic_json(status_path, state)
        source_import._atomic_json(directory / "journal.json", {})
        assert service.status("social") == state
    finally:
        await process.communicate(b"x")
    assert process.returncode == 0
    assert not service.active("social")
    assert service.status("social")["state"] == "recovery_required"
    (directory / "journal.json").unlink()
    assert service.status("social")["state"] == "interrupted"
    state["state"] = "completed"
    source_import._atomic_json(status_path, state)
    assert service.status("social") == state


async def test_task_cancel_during_commit_rolls_back(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    original = store.sync
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_sync(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    store.sync = slow_sync
    task = asyncio.create_task(service.import_data("social", batch("200")))
    await entered.wait()
    assert not service.cancel("social")  # 已经过了可取消阶段
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert docs(store) == {"social:weibo:100"}


async def test_recovery_journal_restores_current_before_retry(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    directory = service._directory("social")
    current = service._snapshot(directory, service._current(directory))
    # 模拟进程在 DB 提交以后、current 发布以前崩溃：DB 是完整但未发布的下一世代。
    (directory / "journal.json").write_text(json.dumps({"fingerprint": current["fingerprint"]}))
    await store.sync([], fingerprint=current["fingerprint"], wiki="social")
    assert service.status("social")["state"] == "recovery_required"
    result = await SourceImportService(service.config, store, embed).import_data("social", batch())
    assert result["noop"]
    assert docs(store) == {"social:weibo:100"}
    assert not (directory / "journal.json").exists()


async def test_disabled_fingerprint_and_unmanaged_data_rejected(tmp_path):
    service, store, embed = setup(tmp_path)
    await store.sync(
        [
            (
                KbChunk("social:legacy", "social:legacy", "entry", "旧文", "旧文", wiki="social"),
                [1, 0],
            )
        ],
        fingerprint="old",
        wiki="social",
    )
    with pytest.raises(SourceImportError, match="管理"):
        await service.import_data("social", batch())
    assert len(store._chunks) == 1
    service.config.files["core.sources"]["sources"][0]["mounted"] = False
    with pytest.raises(SourceImportError, match="停用"):
        await service.import_data("social", batch())


async def test_cli_source_preview_does_not_load_wiki_settings(tmp_path, monkeypatch, capsys):
    service, store, embed = setup(tmp_path)
    path = tmp_path / "input.jsonl"
    path.write_text(batch(), encoding="utf-8")
    args = cli._parser().parse_args(["--source", "social", "preview", str(path)])
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: pytest.fail("不得加载 Wiki 设置"))
    assert await cli._source_command(args, service.config) == 0
    assert json.loads(capsys.readouterr().out)["source_id"] == "social"


async def test_cli_operation_conflict_precedes_input_and_database(tmp_path, monkeypatch, capsys):
    config = conf(tmp_path)
    missing = tmp_path / "missing.jsonl"
    preview_file = tmp_path / "input.jsonl"
    preview_file.write_text(batch(), encoding="utf-8")
    parse = cli._parser().parse_args
    monkeypatch.setattr(cli, "Database", lambda *a, **k: pytest.fail("不得连接数据库"))
    with acquire_source_operation(config, "social"):
        args = parse(["--source", "social", "import", str(missing)])
        assert await cli._source_command(args, config) == 2
        assert capsys.readouterr().err.strip() == str(SourceCollectError("busy"))
        args = parse(["--source", "social", "preview", str(preview_file)])
        assert await cli._source_command(args, config) == 0
        assert json.loads(capsys.readouterr().out)["new"] == 1
        args = parse(["--source", "social", "status"])
        assert await cli._source_command(args, config) == 0
        assert json.loads(capsys.readouterr().out) is None


@pytest.mark.parametrize("changed", [False, True])
def test_cli_import_reloads_config_and_holds_lease_through_shutdown(
    tmp_path, monkeypatch, capsys, changed
):
    config, current = conf(tmp_path), conf(tmp_path)
    config.secrets["postgres"] = {"dsn": "postgresql://test.invalid/test"}
    if changed:
        current.files["core.sources"]["sources"][0]["label"] = "新的标签"
    path = tmp_path / "input.jsonl"
    path.write_text("\n", encoding="utf-8")  # No embedding or external connections needed.
    events = []

    def check_held(event):
        with pytest.raises(SourceCollectError) as error:
            with acquire_source_operation(config, "social"):
                pytest.fail("导入过早释放操作租约")
        assert error.value.code == "busy"
        events.append(event)

    class Database:
        def __init__(self, *args, **kwargs):
            check_held("init_db")

        async def connect(self):
            check_held("connect")

        async def migrate(self):
            check_held("migrate")

        async def close(self):
            check_held("close_db")

    class Service(SourceImportService):
        async def aclose(self):
            check_held("close_service")
            await super().aclose()

    def load(config_dir):
        assert config_dir == str(config.root)
        if not events:
            events.append("load")
            return config
        check_held("reload")
        return current

    monkeypatch.setattr(cli, "load_config", load)
    monkeypatch.setattr(cli, "setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(cli, "Database", Database)
    monkeypatch.setattr(cli, "PostgresKnowledgeStore", lambda db: MemoryKnowledgeStore())
    monkeypatch.setattr(cli, "SourceImportService", Service)
    args = ["--config-dir", str(config.root), "--source", "social", "import", str(path)]
    assert cli.main(args) == (2 if changed else 0)
    output = capsys.readouterr()
    if changed:
        assert "配置已变化" in output.err
    else:
        assert json.loads(output.out)["noop"]
    assert events == [
        "load",
        "init_db",
        "connect",
        "migrate",
        "reload",
        "close_service",
        "close_db",
    ]
    acquire_source_operation(config, "social").close()


async def test_cleanup_has_separate_text_and_image_allowlists(monkeypatch):
    kept = {}

    class DB:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            pass

        async def migrate(self):
            pass

        async def close(self):
            pass

    class Store:
        def __init__(self, db):
            pass

        async def keep_only(self, items):
            kept["text"] = set(items)
            return 0

    class Images(Store):
        async def keep_only(self, items):
            kept["images"] = set(items)
            return 0

    monkeypatch.setattr(cli, "Database", DB)
    monkeypatch.setattr(cli, "PostgresKnowledgeStore", Store)
    monkeypatch.setattr(cli, "PostgresImageStore", Images)
    await cli._forget_other_wikis(
        "fake",
        ["wiki", "disabled_wiki"],
        sources=["wiki", "disabled_wiki", "social", "disabled_social"],
    )
    assert kept["text"] == {"wiki", "disabled_wiki", "social", "disabled_social"}
    assert kept["images"] == {"wiki", "disabled_wiki"}


async def test_database_failure_preserves_snapshot(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    original = store.sync
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated transaction failure")
        return await original(*args, **kwargs)

    store.sync = fail_once
    with pytest.raises(SourceImportError):
        await service.import_data("social", batch("200"))
    assert docs(store) == {"social:weibo:100"}
    assert service.status("social")["state"] == "failed"
    await service.import_data("social", batch("200"))
    assert len(store._chunks) == 2


async def test_default_embedding_verifies_only_changed_content(tmp_path, monkeypatch):
    from types import SimpleNamespace

    service, store, embed = setup(tmp_path)
    verified = []

    async def fake_embed(provider, chunks, vectors):
        await embed(provider, chunks, vectors)

    async def fake_verify(provider, chunks, vectors, *, sample):
        assert sample == 20
        verified.append([item.chunk_id for item in read_embeddable(chunks)])
        return SimpleNamespace(lowest=1.0)

    monkeypatch.setattr(source_import, "embed_chunks", fake_embed)
    monkeypatch.setattr(source_import, "verify_store", fake_verify)
    service.embedder = None
    await service.import_data("social", batch())
    await service.import_data("social", batch("200"))
    assert [len(items) for items in verified] == [1, 1]
    assert verified[0][0] != verified[1][0]
    assert embed.new == 2


async def test_config_mutation_during_embedding_rejected(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    embed.started.clear()
    embed.release = asyncio.Event()
    task = asyncio.create_task(service.import_data("social", batch("200")))
    await embed.started.wait()
    service.config.files["core.sources"]["sources"][0]["role"] = "official"
    embed.release.set()
    with pytest.raises(SourceImportError, match="配置已变化"):
        await task
    assert docs(store) == {"social:weibo:100"}


@pytest.mark.parametrize("change", ["source", "kb_root", "embedding"])
@pytest.mark.parametrize("noop", [False, True])
async def test_reloaded_config_must_match_startup_even_for_noop(tmp_path, change, noop):
    current = conf(tmp_path)
    service, store, embed = setup(tmp_path, config_provider=lambda: current)
    await service.import_data("social", batch())
    before = pointer(service)
    kept = generation_ids(service)
    if change == "source":
        current.files["core.sources"]["sources"][0]["mounted"] = False
    elif change == "kb_root":
        current.files["plugin.knowledge"]["paths"]["root"] = str(tmp_path / "other-kb")
    else:
        current.files["core.embedding"]["providers"]["test"]["text_instruction"] = "新的嵌入指令"

    with pytest.raises(SourceImportError, match="配置已变化"):
        await service.import_data("social", batch() if noop else batch("200"))
    assert service.status("social")["state"] == "failed"
    assert pointer(service) == before
    assert generation_ids(service) == kept
    assert docs(store) == {"social:weibo:100"}
    assert embed.calls == (1 if noop else 2)


async def test_role_label_updates_not_lost_to_idempotence(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    service.config.files["core.sources"]["sources"][0]["role"] = "community"
    result = await service.import_data("social", batch())
    assert result["updated"] == 1
    assert next(iter(store._chunks.values())).meta["source_role"] == "community"


@pytest.mark.parametrize("failure", ["pointer_fsync", "completed_status"])
async def test_failure_after_publication_is_structured_and_does_not_roll_back(
    tmp_path, monkeypatch, failure
):
    service, store, _ = setup(tmp_path)
    await service.import_data("social", batch())
    old_pointer = pointer(service)
    atomic = source_import._atomic_json

    def fail_after_publication(path, value):
        if (
            failure == "completed_status"
            and path.name == "status.json"
            and value["state"] == "completed"
        ):
            raise OSError("internal status write failure at /private/path")
        atomic(path, value)
        if failure == "pointer_fsync" and path.name == "current.json":
            raise OSError("internal fsync failure at /private/path")

    monkeypatch.setattr(source_import, "_atomic_json", fail_after_publication)
    with pytest.raises(SourceImportPublishedError) as error:
        await service.import_data("social", batch("200"))
    assert error.value.published is True
    assert str(error.value) == "资料已发布，但状态记录待确认；请查询状态后重试"
    assert pointer(service) != old_pointer
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}
    status = service.status("social")
    if failure == "pointer_fsync":
        assert status["state"] == "recovery_required"
    else:
        assert status["state"] == "completed" and status["published"] is True

    monkeypatch.setattr(source_import, "_atomic_json", atomic)
    assert (await service.import_data("social", batch("200")))["noop"]
    assert service.status("social")["state"] == "completed"


async def test_recovery_failure_retains_journal_then_retries(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    directory = service._directory("social")
    current = service._snapshot(directory, service._current(directory))
    (directory / "journal.json").write_text(json.dumps({"fingerprint": current["fingerprint"]}))
    original = store.sync

    async def fail(*args, **kwargs):
        raise RuntimeError("database offline")

    store.sync = fail
    with pytest.raises(SourceImportError):
        await service.import_data("social", batch("200"))
    assert service.status("social")["state"] == "recovery_required"
    assert (directory / "journal.json").exists()
    store.sync = original
    await service.import_data("social", batch("200"))
    assert len(store._chunks) == 2


def generation_ids(service):
    root = service._directory("social") / "generations"
    return {path.name for path in root.iterdir()} if root.exists() else set()


def pointer(service):
    return json.loads((service._directory("social") / "current.json").read_text())


@pytest.mark.parametrize(
    "field,code",
    [
        ("max_documents", "documents"),
        ("max_chunks", "chunks"),
        ("max_vector_bytes", "vector_bytes"),
    ],
)
async def test_cumulative_second_batch_rejected_before_embedding(tmp_path, field, code):
    limit = 8 if field == "max_vector_bytes" else 1  # 一个2维向量正好8字节。
    service, store, embed = setup(tmp_path, replace(ImportLimits(), **{field: limit}))
    await service.import_data("social", batch())
    old, old_pointer = dict(store._chunks), pointer(service)
    with pytest.raises(SourceImportCapacityError) as error:
        await service.import_data("social", batch("200"))
    assert error.value.code == code
    assert str(error.value) == CAPACITY_PUBLIC_MESSAGE
    assert error.value.code in CAPACITY_ERROR_CODES
    assert store._chunks == old and pointer(service) == old_pointer
    assert embed.calls == 1
    assert service.status("social")["error_code"] == code
    # 同ID更新不算新增文档，不因容量上限丢弃原帖。
    await service.import_data("social", batch(text="更新旧帖而非删除后新增"))
    assert docs(store) == {"social:weibo:100"}


@pytest.mark.parametrize(
    "field,code",
    [
        ("max_snapshot_bytes", "snapshot_bytes"),
        ("max_working_bytes", "working_bytes"),
        ("max_metadata_bytes", "metadata_bytes"),
    ],
)
async def test_estimated_snapshot_and_memory_budget_precede_embedding(tmp_path, field, code):
    service, store, embed = setup(tmp_path)
    capacity = service.preview("social", batch())["capacity"]
    capacity_key = {
        "max_snapshot_bytes": "snapshot_bytes",
        "max_working_bytes": "working_bytes",
        "max_metadata_bytes": "metadata_bytes",
    }[field]
    service.limits = replace(ImportLimits(), **{field: capacity[capacity_key] - 1})
    with pytest.raises(SourceImportCapacityError) as error:
        await service.import_data("social", batch())
    assert error.value.code == code
    assert embed.calls == 0 and not store._chunks


async def test_model_dimension_budget_uses_configured_dimensions_not_fake_vectors(tmp_path):
    service, store, embed = setup(tmp_path, replace(ImportLimits(), max_vector_bytes=1000))
    service.config.files["core.embedding"]["providers"]["test"]["dimensions"] = 4096
    with pytest.raises(SourceImportCapacityError) as error:
        await service.import_data("social", batch())
    assert error.value.code == "vector_bytes" and embed.calls == 0
    assert not store._chunks


async def test_old_oversized_snapshot_rejected_before_any_json_vector_read(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    service.limits = replace(ImportLimits(), max_snapshot_bytes=1)
    original = source_import._read_json

    def guarded_read(path, *args):
        if path.name == "snapshot.json":
            pytest.fail("大快照必须在读取/JSON解码前拒绝")
        return original(path, *args)

    monkeypatch.setattr(source_import, "_read_json", guarded_read)
    with pytest.raises(SourceImportCapacityError, match="安全容量"):
        service.preview("social", batch("200"))
    with pytest.raises(SourceImportCapacityError):
        await service.import_data("social", batch("200"))
    assert embed.calls == 1 and docs(store) == {"social:weibo:100"}


async def test_snapshot_decode_amplification_rejected_before_load(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    current = service._directory("social") / "generations" / pointer(service)["generation"]
    size = (current / "snapshot.json").stat().st_size
    service.limits = replace(ImportLimits(), max_working_bytes=size * 16 - 1)
    with pytest.raises(SourceImportCapacityError) as error:
        service.preview("social", batch("200"))
    assert error.value.code == "snapshot_read_memory"


async def test_preview_reads_lightweight_documents_only_and_does_not_write(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    directory = service._directory("social")
    before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    original = source_import._read_json

    def without_vectors(path, *args):
        assert path.name != "snapshot.json"
        return original(path, *args)

    monkeypatch.setattr(source_import, "_read_json", without_vectors)
    preview = service.preview("social", batch("200"))
    assert preview["capacity"]["documents"] == 2
    after = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    assert before == after and embed.calls == 1


async def test_disk_shortage_rejected_before_candidate_or_embedding(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    old, old_pointer = dict(store._chunks), pointer(service)
    monkeypatch.setattr(source_import.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(service, "_prepare_stage", lambda *args: pytest.fail("磁盘不足不写候选"))
    with pytest.raises(SourceImportCapacityError) as error:
        await service.import_data("social", batch("200"))
    assert error.value.code == "disk_free_bytes"
    assert store._chunks == old and pointer(service) == old_pointer
    assert embed.calls == 1


async def test_generation_and_reused_vector_cache_are_bounded(tmp_path):
    service, store, embed = setup(tmp_path)
    for revision in range(7):
        await service.import_data("social", batch(text=f"同一帖第{revision}次更新"))
        assert len(generation_ids(service)) <= 2
        current = service._directory("social") / "generations" / pointer(service)["generation"]
        index = (current / "vectors" / "index.jsonl").read_text().splitlines()
        assert len(index) == 1  # 不把已不存在的历史切片向量复制到下一世代。
    saved = pointer(service)
    assert generation_ids(service) == {saved["generation"], saved["previous"]}
    assert docs(store) == {"social:weibo:100"}
    for post_id in range(200, 205):
        await service.import_data("social", batch(str(post_id)))
        assert len(generation_ids(service)) == 2
    assert docs(store) == {"social:weibo:100", *(f"social:weibo:{i}" for i in range(200, 205))}
    current = service._directory("social") / "generations" / pointer(service)["generation"]
    assert len((current / "vectors" / "index.jsonl").read_text().splitlines()) == 6


async def test_gc_after_cancel_preserves_current_and_rollback(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    await service.import_data("social", batch("200"))
    kept = generation_ids(service)
    embed.started.clear()
    embed.release = asyncio.Event()
    task = asyncio.create_task(service.import_data("social", batch("300")))
    await embed.started.wait()
    assert service.cancel("social")
    embed.release.set()
    assert (await task)["state"] == "cancelled"
    assert generation_ids(service) == kept
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}


async def test_thread_cancellation_waits_before_unlock_and_gc(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    await service.import_data("social", batch("200"))
    kept = generation_ids(service)
    entered, release = threading.Event(), threading.Event()
    prepare = service._prepare_stage

    def blocked_prepare(*args):
        entered.set()
        assert release.wait(timeout=5)
        prepare(*args)

    monkeypatch.setattr(service, "_prepare_stage", blocked_prepare)
    task = asyncio.create_task(service.import_data("social", batch("300")))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)  # 仅让取消回调调度，不等待真实外部服务。
    other = SourceImportService(service.config, store, embed)
    try:
        with pytest.raises(SourceImportError, match="另一个"):
            await other.import_data("social", batch("400"))
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert generation_ids(service) == kept
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}


async def test_gc_after_rollback_preserves_previous_pointer_references(tmp_path, monkeypatch):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    await service.import_data("social", batch("200"))
    kept, saved = generation_ids(service), pointer(service)
    atomic = source_import._atomic_json

    def fail_current(path, value):
        if path.name == "current.json":
            raise OSError("publication failed")
        atomic(path, value)

    monkeypatch.setattr(source_import, "_atomic_json", fail_current)
    with pytest.raises(SourceImportError):
        await service.import_data("social", batch("300"))
    assert generation_ids(service) == kept and pointer(service) == saved
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}


async def test_pending_journal_prevents_gc_then_recovery_reclaims_only_unreferenced(tmp_path):
    import fcntl

    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    await service.import_data("social", batch("200"))
    kept = generation_ids(service)
    directory = service._directory("social")
    candidate, stale = uuid.uuid4().hex, uuid.uuid4().hex
    for generation in (candidate, stale):
        (directory / "generations" / generation).mkdir()
    (directory / "journal.json").write_text(
        json.dumps(
            {
                "candidate": candidate,
                "previous": pointer(service)["generation"],
                "fingerprint": "test:fixed-model:2",
            }
        )
    )
    with (directory / "import.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert service._gc(directory)["cleanup_pending"]
    assert generation_ids(service) == kept | {candidate, stale}
    await service.import_data("social", batch("200"))
    assert generation_ids(service) == kept
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}


async def test_gc_failure_is_reported_without_deleting_referenced_generations(
    tmp_path, monkeypatch
):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    first = pointer(service)["generation"]
    await service.import_data("social", batch("200"))
    remove = source_import.shutil.rmtree
    attempted = []

    def fail_gc(path):
        attempted.append(path.name)
        raise OSError("simulated GC permission failure")

    monkeypatch.setattr(source_import.shutil, "rmtree", fail_gc)
    result = await service.import_data("social", batch("300"))
    assert result["state"] == "completed" and result["cleanup_pending"]
    refs = pointer(service)
    assert attempted == [first]
    assert {refs["generation"], refs["previous"]} <= generation_ids(service)
    assert len(store._chunks) == 3
    monkeypatch.setattr(source_import.shutil, "rmtree", remove)
    await service.import_data("social", batch("300"))
    assert generation_ids(service) == {refs["generation"], refs["previous"]}


async def test_gc_never_follows_uuid_symlink_or_non_uuid_path(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    directory = service._directory("social")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "must-stay"
    sentinel.write_text("unchanged")
    linked = directory / "generations" / uuid.uuid4().hex
    linked.symlink_to(external, target_is_directory=True)
    unknown = directory / "generations" / "not-a-generation"
    unknown.mkdir()
    await service.import_data("social", batch("200"))
    assert sentinel.read_text() == "unchanged"
    assert linked.is_symlink() and unknown.exists()
    assert service.status("social")["cleanup_pending"]


@pytest.mark.parametrize(
    "field,value", [("max_documents", 0), ("max_chunks", True), ("max_snapshot_bytes", -1)]
)
def test_import_limits_are_typed_positive_budgets(field, value):
    with pytest.raises(ValueError):
        replace(ImportLimits(), **{field: value})


async def test_legacy_generation_without_metadata_remains_readable_within_budget(tmp_path):
    service, store, embed = setup(tmp_path)
    await service.import_data("social", batch())
    original = pointer(service)["generation"]
    metadata = service._directory("social") / "generations" / original / "documents.json"
    metadata.unlink()  # 模拟首版只有完整snapshot的世代，而非丢弃任何来源资料。
    preview = service.preview("social", batch("200"))
    assert preview["new"] == 1 and preview["total_documents"] == 2
    await service.import_data("social", batch("200"))
    assert pointer(service)["previous"] == original
    assert docs(store) == {"social:weibo:100", "social:weibo:200"}
    assert embed.new == 2  # 首版世代向量也复用，不重嵌旧帖。
