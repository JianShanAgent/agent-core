"""控制/worker 离线回归，假 transport/embedding 不代表真实站点采集。"""

import asyncio
import copy
import json
import multiprocessing

import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.plugins.knowledge import source_collect as control
from jianshanagent.plugins.knowledge.source_collect_runner import SourceCollectorRunner
from jianshanagent.plugins.knowledge.source_import import SourceImportService, _atomic_json
from jianshanagent.plugins.knowledge.store import MemoryKnowledgeStore
from jianshanagent.weibo_collect import WeiboCollectError, WeiboFetchBatch
from test_source_import import FakeEmbedder, conf

UID = "5896401674"


def configured(tmp_path, *, enabled=False):
    config = conf(tmp_path)
    files = copy.deepcopy(config.files)
    files["core.sources"]["sources"][0]["collector"] = {
        "uid": UID,
        "enabled": enabled,
    }
    return AppConfig(config.root, files, config.secrets)


def row(post_id="100", text="离线微博正文，仅用于单元测试"):
    return {
        "version": 1,
        "id": post_id,
        "text": text,
        "author_id": UID,
        "url": f"https://weibo.com/{UID}/Abc{post_id}",
    }


def batch(rows=None, *, complete=True, checkpoint=None):
    values = [row()] if rows is None else rows
    return WeiboFetchBatch(
        values, checkpoint or {"cursor": "safe"}, complete, len(values), 0, 1, not complete
    )


class Harness:
    def __init__(self, config):
        self.config = config
        self.store = MemoryKnowledgeStore()
        self.embedder = FakeEmbedder()
        self.fetch_calls = []
        self.result = batch()
        self.fail = None
        self.started = asyncio.Event()
        self.hold = None
        self.http_cancelled = False
        self.now = 1_800_000_000.0

    async def service(self, config):
        service = SourceImportService(
            config, self.store, self.embedder, config_provider=lambda: self.config
        )

        async def close():
            pass

        return service, close

    async def fetch(self, settings, credentials, *, checkpoint=None, progress=None):
        self.fetch_calls.append(checkpoint)
        self.started.set()
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.fail:
                raise self.fail
            if progress:
                progress(
                    {
                        "pages": 1,
                        "fetched": len(self.result.rows),
                        "message": "Cookie=DO_NOT_EXPOSE",
                    }
                )
            return self.result
        except asyncio.CancelledError:
            self.http_cancelled = True
            raise

    def runner(self):
        return SourceCollectorRunner(
            lambda: self.config,
            clock=lambda: self.now,
            fetcher=self.fetch,
            service_factory=self.service,
        )


async def finish(runner):
    if runner.tasks:
        await asyncio.gather(*tuple(runner.tasks.values()))
    await asyncio.sleep(0)


def _child_operation(config, connection):
    try:
        with control.acquire_source_operation(config, "social"):
            connection.send("acquired")
    except control.SourceCollectError as error:
        connection.send(error.code)
    finally:
        connection.close()


def _child_cancel(config, job_id, connection):
    try:
        connection.send(control.cancel_collect(config, "social", job_id))
    finally:
        connection.close()


def _cancel_from_other_process(config, job_id):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child_cancel, args=(config, job_id, sender))
    process.start()
    sender.close()
    assert receiver.poll(10)
    result = receiver.recv()
    process.join(10)
    assert process.exitcode == 0
    return result


def test_operation_lease_is_immediate_nonblocking_and_cross_process(tmp_path):
    config = configured(tmp_path)
    held = control.acquire_source_operation(config, "social")
    assert control.source_operation_active(config, "social")
    assert not control.collector_active(config, "social")  # 操作锁与采集器运行状态独立。
    with pytest.raises(control.SourceCollectError, match="已有"):
        control.acquire_source_operation(config, "social")
    # spawn 不继承父进程持有的 fd；证明真实 flock，而非本进程 task 字典。
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child_operation, args=(config, sender))
    process.start()
    sender.close()
    assert receiver.poll(10)
    assert receiver.recv() == "busy"
    process.join(10)
    assert process.exitcode == 0
    held.close()
    held.close()
    with control.acquire_source_operation(config, "social"):
        pass


def test_request_cancel_durable_job_id_and_new_job(tmp_path):
    config = configured(tmp_path)
    requested = control.request_collect(config, "social")
    assert requested["state"] == "queued"
    assert control.collector_active(config, "social")
    assert control.collector_overview(config, "social")["job_id"] == requested["job_id"]
    with pytest.raises(control.SourceCollectError) as caught:
        control.cancel_collect(config, "social", "0" * 32)
    assert caught.value.code == "stale_job"
    with pytest.raises(control.SourceCollectError):
        control.request_collect(config, "social")
    assert control.cancel_collect(config, "social", requested["job_id"])["state"] == "cancelled"
    second = control.request_collect(config, "social")
    assert second["job_id"] != requested["job_id"]
    with pytest.raises(control.SourceCollectError):
        control.cancel_collect(config, "social", requested["job_id"])
    assert control.collector_overview(config, "social")["state"] == "queued"


def test_stale_status_and_sensitive_values_never_leak(tmp_path):
    config = configured(tmp_path)
    directory = control._directory(config, "social")
    directory.mkdir(parents=True)
    _atomic_json(
        directory / "status.json",
        {
            "state": "fetching",
            "phase": "fetching",
            "job_id": "1" * 32,
            "message": "Cookie=TOP_SECRET",
            "error_code": "authorization_url=secret",
            "raw_response": "private",
            "uid": "https://attacker/secret",
        },
    )
    status = control.collector_overview(config, "social")
    assert status["state"] == "interrupted"
    assert control._read(directory / "status.json")["state"] == "fetching"  # 查询不改磁盘。
    assert not control.collector_active(config, "social")
    assert "secret" not in json.dumps(status).lower()
    assert "TOP_SECRET" not in json.dumps(status)
    assert status["profile_url"] == f"https://weibo.com/u/{UID}"
    with control._acquire(directory / "collector.lock"):
        assert control.collector_overview(config, "social")["state"] == "fetching"


async def test_automatic_first_round_clock_and_idempotent_next_round(tmp_path):
    harness = Harness(configured(tmp_path, enabled=True))
    runner = harness.runner()
    await runner.tick()
    await finish(runner)
    first = control.collector_overview(harness.config, "social")
    assert first["state"] == "completed"
    assert first["new"] == first["accepted"] == 1
    assert first["next_run_at"] == harness.now + 1800
    assert harness.embedder.new == 1
    await runner.tick()
    assert not runner.tasks
    harness.now += 1800
    await runner.tick()
    await finish(runner)
    second = control.collector_overview(harness.config, "social")
    assert second["new"] == second["updated"] == 0
    assert second["unchanged"] == 1
    assert harness.embedder.new == 1
    assert harness.fetch_calls == [None, {"cursor": "safe"}]
    assert "Cookie" not in json.dumps(second)


async def test_manual_enabled_false_and_two_workers_compete(tmp_path):
    harness = Harness(configured(tmp_path))
    harness.hold = asyncio.Event()
    first, second = harness.runner(), harness.runner()
    await first.tick()
    assert not first.tasks
    requested = control.request_collect(harness.config, "social")
    await asyncio.gather(first.tick(), second.tick())
    await harness.started.wait()
    assert len(harness.fetch_calls) == 1
    assert bool(first.tasks) != bool(second.tasks)
    assert control.collector_overview(harness.config, "social")["job_id"] == requested["job_id"]
    with pytest.raises(control.SourceCollectError):
        control.acquire_source_operation(harness.config, "social")
    harness.hold.set()
    await finish(first)
    await finish(second)
    assert control.collector_overview(harness.config, "social")["next_run_at"] is None


async def test_cancel_running_http_cross_worker_and_no_checkpoint(tmp_path):
    harness = Harness(configured(tmp_path))
    harness.hold = asyncio.Event()
    requested = control.request_collect(harness.config, "social")
    runner = harness.runner()
    await runner.tick()
    await harness.started.wait()
    status = await asyncio.to_thread(
        _cancel_from_other_process, harness.config, requested["job_id"]
    )
    assert status["state"] == "stopping"
    await finish(runner)
    assert harness.http_cancelled
    assert not harness.store._chunks
    assert control.collector_overview(harness.config, "social")["state"] == "cancelled"
    assert not (control._directory(harness.config, "social") / "checkpoint.json").exists()
    assert not control.collector_active(harness.config, "social")


@pytest.mark.parametrize("change", ["mounted", "role", "initial_days", "uid"])
async def test_configuration_change_cancels_http_without_publishing(tmp_path, change):
    harness = Harness(configured(tmp_path))
    harness.hold = asyncio.Event()
    control.request_collect(harness.config, "social")
    runner = harness.runner()
    await runner.tick()
    await harness.started.wait()
    files = copy.deepcopy(harness.config.files)
    target = files["core.sources"]["sources"][0]
    if change == "mounted":
        target["mounted"] = False
    elif change == "role":
        target["role"] = "official"
    else:
        target["collector"][change] = "123456789" if change == "uid" else 10
    harness.config = AppConfig(harness.config.root, files, harness.config.secrets)
    await finish(runner)
    status = control.collector_overview(harness.config, "social")
    assert status["state"] == "failed"
    assert status["error_code"] == "config_changed"
    assert harness.http_cancelled
    assert not harness.store._chunks


async def test_partial_checkpoint_only_after_publish_failure_retains_old(tmp_path):
    harness = Harness(configured(tmp_path))
    harness.result = batch(complete=False, checkpoint={"pending": "first"})
    runner = harness.runner()
    control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    directory = control._directory(harness.config, "social")
    old_checkpoint = (directory / "checkpoint.json").read_bytes()
    importer_dir = harness.config.files["plugin.knowledge"]["paths"]["root"]
    from pathlib import Path

    pointer = Path(importer_dir) / "social/source-import/current.json"
    old_current = pointer.read_bytes()
    harness.result = batch([row("101")], checkpoint={"pending": "second"})
    harness.embedder.fail = True
    control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    assert (directory / "checkpoint.json").read_bytes() == old_checkpoint
    assert pointer.read_bytes() == old_current
    assert harness.fetch_calls[-1] == {"pending": "first"}
    assert control.collector_overview(harness.config, "social")["state"] == "failed"
    harness.embedder.fail = False
    control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    assert control._read(directory / "checkpoint.json")["checkpoint"] == {"pending": "second"}
    assert len(harness.store._chunks) == 2  # 本批未出现的旧帖保留。


async def test_no_pg_no_fetch_or_fake_success(tmp_path):
    config = configured(tmp_path)
    control.request_collect(config, "social")
    runner = SourceCollectorRunner(lambda: config)
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(config, "social")
    assert status["state"] == "failed"
    assert status["error_code"] == "database_unavailable"
    assert status["last_success_at"] is None


async def test_session_expired_does_not_advance_checkpoint(tmp_path):
    harness = Harness(configured(tmp_path))
    harness.fail = WeiboCollectError("authentication_required")
    control.request_collect(harness.config, "social")
    runner = harness.runner()
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(harness.config, "social")
    assert status["state"] == "authentication_required"
    assert status["error_code"] == "authentication_required"
    assert not (control._directory(harness.config, "social") / "checkpoint.json").exists()


async def test_stale_queued_configuration_rejected_before_fetch(tmp_path):
    harness = Harness(configured(tmp_path))
    control.request_collect(harness.config, "social")
    harness.config.files["core.sources"]["sources"][0]["collector"]["uid"] = "123456789"
    runner = harness.runner()
    await runner.tick()
    assert not harness.fetch_calls
    assert not runner.tasks
    assert control.collector_overview(harness.config, "social")["error_code"] == "config_changed"


async def test_shutdown_cancels_http_and_releases_real_lease(tmp_path):
    harness = Harness(configured(tmp_path))
    harness.hold = asyncio.Event()
    control.request_collect(harness.config, "social")
    runner = harness.runner()
    await runner.tick()
    await harness.started.wait()
    await runner.aclose()
    assert harness.http_cancelled
    assert not control.collector_active(harness.config, "social")
    assert control.collector_overview(harness.config, "social")["state"] == "cancelled"


async def test_cancel_while_embedding_preserves_snapshot_and_checkpoint(tmp_path):
    harness = Harness(configured(tmp_path))
    runner = harness.runner()
    control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    directory = control._directory(harness.config, "social")
    old_checkpoint = (directory / "checkpoint.json").read_bytes()
    harness.embedder.started.clear()
    harness.embedder.release = asyncio.Event()
    harness.result = batch([row("101")], checkpoint={"cursor": "not-published"})
    requested = control.request_collect(harness.config, "social")
    await runner.tick()
    await harness.embedder.started.wait()
    control.cancel_collect(harness.config, "social", requested["job_id"])
    # runner先把取消交给inner service，等嵌入安全返回后再释放锁。
    await asyncio.sleep(0.3)
    assert control.collector_active(harness.config, "social")
    harness.embedder.release.set()
    await finish(runner)
    assert control.collector_overview(harness.config, "social")["state"] == "cancelled"
    assert (directory / "checkpoint.json").read_bytes() == old_checkpoint
    assert len(harness.store._chunks) == 1


async def test_cancel_committing_waits_for_real_publish_and_does_not_advance_cursor(tmp_path):
    harness = Harness(configured(tmp_path))
    started, release = asyncio.Event(), asyncio.Event()
    real_sync = harness.store.sync

    async def held_sync(records, *, fingerprint, wiki):
        started.set()
        await release.wait()
        return await real_sync(records, fingerprint=fingerprint, wiki=wiki)

    harness.store.sync = held_sync
    requested = control.request_collect(harness.config, "social")
    runner = harness.runner()
    await runner.tick()
    await started.wait()
    control.cancel_collect(harness.config, "social", requested["job_id"])
    await asyncio.sleep(0.3)
    assert control.collector_overview(harness.config, "social")["state"] == "stopping"
    assert control.collector_active(harness.config, "social")
    release.set()
    await finish(runner)
    status = control.collector_overview(harness.config, "social")
    assert status["state"] == "completed"  # 实际已发布，不能虚报取消成功。
    assert len(harness.store._chunks) == 1
    assert not (control._directory(harness.config, "social") / "checkpoint.json").exists()
    assert not control.collector_active(harness.config, "social")


async def test_pg_failure_preserves_checkpoint_and_never_fetches(tmp_path):
    harness = Harness(configured(tmp_path))
    runner = harness.runner()
    control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    checkpoint = control._directory(harness.config, "social") / "checkpoint.json"
    before = checkpoint.read_bytes()

    async def unavailable():
        raise RuntimeError("postgres://secret:password@private")

    harness.store.info = unavailable
    requested = control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(harness.config, "social")
    assert status["job_id"] == requested["job_id"]
    assert status["error_code"] == "database_unavailable"
    assert checkpoint.read_bytes() == before
    assert len(harness.fetch_calls) == 1
    assert "password" not in json.dumps(status)


async def test_disabled_target_does_not_auto_run_and_queued_role_change_is_rejected(tmp_path):
    harness = Harness(configured(tmp_path, enabled=True))
    target = harness.config.files["core.sources"]["sources"][0]
    target["mounted"] = False
    runner = harness.runner()
    await runner.tick()
    assert not runner.tasks
    with pytest.raises(control.SourceCollectError):
        control.request_collect(harness.config, "social")
    target["mounted"] = True
    control.request_collect(harness.config, "social")
    target["role"] = "official"
    await runner.tick()
    assert not runner.tasks
    assert not harness.fetch_calls
    assert control.collector_overview(harness.config, "social")["error_code"] == "config_changed"


async def test_sync_collector_runs_while_long_wiki_round_is_blocked(tmp_path, monkeypatch):
    import signal
    from types import SimpleNamespace

    from jianshanagent.plugins.knowledge import sync
    from jianshanagent.plugins.knowledge.settings import SyncSettings

    harness = Harness(configured(tmp_path, enabled=True))
    harness.config.files["core.wiki"]["wikis"] = [
        {"id": "huiji", "platform": "huiji", "api_url": "https://wiki.test/api.php"}
    ]
    harness.hold = asyncio.Event()
    wiki_started, wiki_release = asyncio.Event(), asyncio.Event()
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(sync, "load_config", lambda directory: harness.config)
    monkeypatch.setattr(sync, "SourceCollectorRunner", lambda provider: harness.runner())
    monkeypatch.setattr(
        sync, "load_settings", lambda config, source: SimpleNamespace(sync=SyncSettings())
    )
    monkeypatch.setattr(sync, "downloaded_sites_dir", lambda path: None)
    monkeypatch.setattr(sync, "clear_caches", lambda: None)
    monkeypatch.setattr(sync, "restart_needed", lambda: False)

    async def long_wiki(*args, **kwargs):
        wiki_started.set()
        await wiki_release.wait()
        return {}

    monkeypatch.setattr(sync, "run_wikis", long_wiki)
    task = asyncio.create_task(sync.run_loop())
    try:
        await asyncio.wait_for(wiki_started.wait(), 3)
        await asyncio.wait_for(harness.started.wait(), 3)
        assert len(harness.fetch_calls) == 1
        assert not wiki_release.is_set()
    finally:
        handlers[signal.SIGTERM]()
        wiki_release.set()
        assert await asyncio.wait_for(task, 3) == 0
    assert harness.http_cancelled


async def test_checkpoint_failure_does_not_claim_published_documents_rolled_back(
    tmp_path, monkeypatch
):
    from jianshanagent.plugins.knowledge import source_collect_runner

    harness = Harness(configured(tmp_path))
    runner = harness.runner()
    control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    checkpoint = control._directory(harness.config, "social") / "checkpoint.json"
    before = checkpoint.read_bytes()
    harness.result = batch([row("101")], checkpoint={"cursor": "after-publish"})
    real_write = source_collect_runner._atomic_json

    def fail_checkpoint(path, value):
        if path.name == "checkpoint.json":
            raise OSError("private-disk-path")
        return real_write(path, value)

    monkeypatch.setattr(source_collect_runner, "_atomic_json", fail_checkpoint)
    control.request_collect(harness.config, "social")
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(harness.config, "social")
    assert status["error_code"] == "publication_uncertain"
    assert "已发布" in status["message"]
    assert "保持不变" not in status["message"]
    assert status["new"] == 1
    assert status["last_success_at"] == harness.now
    assert len(harness.store._chunks) == 2
    assert checkpoint.read_bytes() == before
    assert not control.collector_active(harness.config, "social")


async def test_importer_post_publish_state_failure_is_not_reported_as_rollback(
    tmp_path, monkeypatch
):
    from jianshanagent.plugins.knowledge import source_import

    harness = Harness(configured(tmp_path))
    real_write = source_import._atomic_json

    def fail_completed_status(path, value):
        if (
            path.name == "status.json"
            and path.parent.name == "source-import"
            and value.get("state") == "completed"
        ):
            raise OSError("sensitive-status-storage-path")
        return real_write(path, value)

    monkeypatch.setattr(source_import, "_atomic_json", fail_completed_status)
    control.request_collect(harness.config, "social")
    runner = harness.runner()
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(harness.config, "social")
    assert status["error_code"] == "publication_uncertain"
    assert "保持不变" not in status["message"]
    assert len(harness.store._chunks) == 1
    assert not (control._directory(harness.config, "social") / "checkpoint.json").exists()
    assert "sensitive" not in json.dumps(status)
