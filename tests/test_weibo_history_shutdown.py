"""Worker shutdown is resumable; only explicit controls create a durable pause."""

import asyncio

import pytest

from jianshanagent.plugins.knowledge import source_collect as control
from jianshanagent.plugins.knowledge.source_collect_runner import SourceCollectorRunner
from jianshanagent.weibo_history_queue import HistoryQueue
from test_weibo_history import STAMP, UID, finish, runner_harness


def pending(config):
    directory = control._directory(config, "social")
    settings = control._settings(config, "social")
    with HistoryQueue(
        directory / "history.sqlite3", UID, control._signature(settings), now=STAMP
    ) as queue:
        return queue.pending(), queue.job(), queue.status()


def restart(config, clock, old):
    return SourceCollectorRunner(
        lambda: config, clock=lambda: clock.now, service_factory=old.service_factory
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_worker_shutdown_replays_pending_without_manual_resume(
    tmp_path, monkeypatch, enabled
):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    config.files["core.sources"]["sources"][0]["collector"]["enabled"] = enabled
    if not enabled:
        await runner.tick()
        assert not calls
        control.request_collect(config, "social")
    service.hold = asyncio.Event()
    await runner.tick()
    await asyncio.wait_for(service.started.wait(), 2)
    candidate, job, status = pending(config)
    assert candidate is not None and job["cursor"] == "" and status["posts_total"] == 0

    await asyncio.wait_for(runner.aclose(), 2)
    interrupted = control.collector_overview(config, "social")
    assert interrupted["state"] == "interrupted" and not interrupted["paused"]
    assert interrupted["history_phase"] == "posts"
    assert interrupted["next_batch_at"] == clock.now + 2
    assert (
        control._read(control._directory(config, "social") / "pause.json").get("paused") is not True
    )
    after, job, status = pending(config)
    assert after == candidate and job["cursor"] == "" and not status["paused"]

    clock.now += 2
    resumed = restart(config, clock, runner)
    await resumed.tick()
    await finish(resumed)
    assert len(calls) == 2
    assert service.imported[0][1] == service.imported[1][1]
    assert control.collector_overview(config, "social")["posts_total"] == 1
    assert pending(config)[0] is None
    await resumed.aclose()


async def test_user_pause_during_shutdown_drain_wins_and_stays_durable(tmp_path, monkeypatch):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    service.hold = asyncio.Event()
    draining, release = asyncio.Event(), asyncio.Event()

    async def drain():
        draining.set()
        await release.wait()

    service.aclose = drain
    await runner.tick()
    await asyncio.wait_for(service.started.wait(), 2)
    closing = asyncio.create_task(runner.aclose())
    await asyncio.wait_for(draining.wait(), 2)
    interrupted = control.collector_overview(config, "social")
    assert interrupted["state"] == "interrupted"
    paused = control.cancel_collect(config, "social", interrupted["job_id"])
    assert paused["paused"] and paused["state"] == "paused"
    release.set()
    await asyncio.wait_for(closing, 2)

    state = control.collector_overview(config, "social")
    assert state["paused"] and state["history_phase"] == "paused"
    candidate, job, queue_status = pending(config)
    assert candidate is not None and job["cursor"] == "" and queue_status["paused"]
    assert control._read(control._directory(config, "social") / "pause.json")["paused"]
    clock.now += 3600
    resumed = restart(config, clock, runner)
    await resumed.tick()
    assert not resumed.tasks and len(calls) == 1
    await resumed.aclose()


async def test_config_change_still_blocks_when_worker_is_closing(tmp_path, monkeypatch):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    service.hold = asyncio.Event()
    cancellation = asyncio.Event()

    def cancel_without_releasing(target):
        cancellation.set()

    service.cancel = cancel_without_releasing
    await runner.tick()
    await asyncio.wait_for(service.started.wait(), 2)
    closing = asyncio.create_task(runner.aclose())
    await asyncio.wait_for(cancellation.wait(), 2)
    config.files["core.sources"]["sources"][0]["role"] = "community"
    service.hold.set()
    await asyncio.wait_for(closing, 2)
    blocked = control.collector_overview(config, "social")
    assert blocked["state"] == "blocked" and blocked["error_code"] == "config_changed"
    assert blocked["paused"] and pending(config)[0] is not None
    clock.now += 3600
    resumed = restart(config, clock, runner)
    await resumed.tick()
    assert not resumed.tasks and len(calls) == 1
    await resumed.aclose()


async def test_shutdown_during_commit_drains_without_rolling_back_published_content(
    tmp_path, monkeypatch
):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    committed, release, cancellation = asyncio.Event(), asyncio.Event(), asyncio.Event()
    published = {}
    changes = 0
    rolled_back = False

    async def shielded_commit(target, content):
        nonlocal changes, rolled_back
        if published.get(target) != content:
            published[target] = content  # Synthetic PG commit boundary, before pointer/drain.
            changes += 1
        committed.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            rolled_back = True
            published.pop(target, None)
            raise
        return {"state": "completed", "new": 1}

    def cancel(target):
        cancellation.set()  # Cancellation is a request; it cannot cancel an in-flight commit.

    service.import_data = shielded_commit
    service.cancel = cancel
    service.status = lambda target: {"state": "committing"}
    await runner.tick()
    await asyncio.wait_for(committed.wait(), 2)
    before = dict(published)
    closing = asyncio.create_task(runner.aclose())
    await asyncio.wait_for(cancellation.wait(), 2)
    assert not closing.done() and published == before
    candidate, job, _ = pending(config)
    assert candidate is not None and job["cursor"] == ""
    release.set()
    await asyncio.wait_for(closing, 2)
    assert not rolled_back and published == before
    assert control.collector_overview(config, "social")["state"] == "interrupted"
    assert pending(config)[0] == candidate

    clock.now += 2
    resumed = restart(config, clock, runner)
    await resumed.tick()
    await finish(resumed)
    assert changes == 1 and not rolled_back and published == before
    assert control.collector_overview(config, "social")["posts_total"] == 1
    assert pending(config)[0] is None
    await resumed.aclose()
