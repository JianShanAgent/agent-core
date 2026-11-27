"""仅 kbsync 调用的独立采集调度；不使用 Wiki 的长轮询周期。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from jianshanagent.core.config import AppConfig
from jianshanagent.core.db import Database
from jianshanagent.core.sources import source_instances
from jianshanagent.plugins.knowledge.source_collect import (
    _MESSAGES,
    SourceCollectError,
    SourceOperationLease,
    _acquire,
    _config_signature,
    _control,
    _directory,
    _held,
    _read,
    _settings,
    _signature,
    acquire_source_operation,
)
from jianshanagent.plugins.knowledge.source_import import (
    SourceImportCapacityError,
    SourceImportService,
    _atomic_json,
    _blocking,
)
from jianshanagent.plugins.knowledge.store import PostgresKnowledgeStore
from jianshanagent.weibo_auth import WeiboCredentialStore

POLL_SECONDS = 5.0
ServiceFactory = Callable[
    [AppConfig], Awaitable[tuple[SourceImportService, Callable[[], Awaitable[None]]]]
]


class SourceCollectorRunner:
    def __init__(
        self,
        config_provider: Callable[[], AppConfig],
        *,
        clock: Callable[[], float] = time.time,
        fetcher: Callable[..., Awaitable[Any]] | None = None,
        service_factory: ServiceFactory | None = None,
    ):
        self.config_provider = config_provider
        self.clock = clock
        self.fetcher = fetcher
        self.service_factory = service_factory or self._service
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    async def _service(
        self, config: AppConfig
    ) -> tuple[SourceImportService, Callable[[], Awaitable[None]]]:
        if not config.postgres_dsn:
            raise SourceCollectError("database_unavailable", status=503)
        db = Database(config.postgres_dsn, pool_max=2)
        try:
            await asyncio.wait_for(db.connect(), 20)
        except BaseException:
            with contextlib.suppress(Exception):
                await db.close()
            raise SourceCollectError("database_unavailable", status=503) from None
        return (
            SourceImportService(
                config, PostgresKnowledgeStore(db), config_provider=self.config_provider
            ),
            db.close,
        )

    async def tick(self) -> None:
        """短操作：发起独立 job，不等待 HTTP/嵌入/数据库发布。"""
        if self._closing:
            return
        config = self.config_provider()
        for source in source_instances(config):
            if source.driver != "documents" or source.platform.id != "weibo":
                continue
            try:
                settings = _settings(config, source.id)
            except SourceCollectError:
                continue
            if source.id in self.tasks:
                continue
            lease: SourceOperationLease | None = None
            collector: SourceOperationLease | None = None
            try:
                directory = _directory(config, source.id)
                if _held(directory / "collector.lock"):
                    continue
                lease = acquire_source_operation(config, source.id)
                with _control(config, source.id):
                    state = _read(directory / "status.json")
                    request = _read(directory / "request.json")
                    requested = bool(request and request.get("state") == "queued")
                    full = settings.get("mode") == "full_then_incremental"
                    if (
                        full
                        and not requested
                        and (
                            _read(directory / "pause.json").get("paused") is True
                            or state.get("history_phase") in {"paused", "blocked"}
                        )
                    ):
                        continue
                    schedule_signature = json.dumps(settings, sort_keys=True)
                    schedule_changed = state.get("schedule_signature") != schedule_signature
                    continuing_history = (
                        full
                        and state.get("history_started_at") is not None
                        and state.get("history_complete") is not True
                    )
                    due = self.clock() >= (
                        state.get("next_batch_at") or state.get("next_run_at") or 0
                    )
                    if not requested and not (
                        source.mounted
                        and (
                            settings["enabled"]
                            and (schedule_changed or due)
                            or continuing_history
                            and due
                        )
                    ):
                        continue
                    if not source.mounted:
                        self._reject_request(directory, request, "config_changed")
                        continue
                    if requested and (
                        request.get("signature") != _signature(settings)
                        or request.get("config_signature") != _config_signature(config, source.id)
                    ):
                        self._reject_request(directory, request, "config_changed")
                        continue
                    collector = _acquire(directory / "collector.lock")
                    value = {
                        **{
                            key: state[key]
                            for key in state
                            if key.startswith("history_")
                            or key
                            in {
                                "snapshot_at",
                                "posts_total",
                                "comments_total",
                                "replies_total",
                                "pending_posts",
                                "pending_comments",
                                "pending_replies",
                                "index_pending",
                                "inaccessible",
                                "count_mismatches",
                                "oldest_date",
                                "resource_bytes",
                                "vector_bytes",
                                "measurement",
                                "coverage_message",
                            }
                        },
                        "manual": requested,
                        "source_id": source.id,
                        "job_id": request["job_id"] if requested else uuid.uuid4().hex,
                        "uid": settings["uid"],
                        "profile_url": f"https://weibo.com/u/{settings['uid']}",
                        "state": "fetching",
                        "phase": "fetching",
                        "pages": 0,
                        "fetched": 0,
                        "accepted": 0,
                        "new": 0,
                        "updated": 0,
                        "unchanged": 0,
                        "started_at": self.clock(),
                        "finished_at": None,
                        "heartbeat_at": self.clock(),
                        "last_success_at": state.get("last_success_at"),
                        "next_run_at": None,
                        "error_code": None,
                        "message": "正在采集公开微博。",
                        "limited": False,
                        "schedule_signature": schedule_signature,
                    }
                    _atomic_json(directory / "status.json", value)
                    (directory / "request.json").unlink(missing_ok=True)
                task = asyncio.create_task(
                    self._run(config, source.id, settings, value, lease, collector)
                )
                self.tasks[source.id] = task
                task.add_done_callback(partial(self._finished, source.id))
                lease = collector = None
            except SourceCollectError:
                # 操作竞争属于正常情况；持久请求不被消耗，下个 ticker 再看。
                continue
            finally:
                if collector is not None:
                    collector.close()
                if lease is not None:
                    lease.close()

    def _finished(self, source_id: str, task: asyncio.Task[None]) -> None:
        if self.tasks.get(source_id) is task:
            self.tasks.pop(source_id, None)
        if not task.cancelled():
            # 即便磁盘失效导致状态无法记录，也必须读取异常；不把原始异常写到公开日志。
            task.exception()

    def _reject_request(self, directory: Path, request: dict[str, Any], code: str) -> None:
        _atomic_json(
            directory / "status.json",
            {
                **request,
                "state": "failed",
                "phase": "failed",
                "error_code": code,
                "message": _MESSAGES[code],
                "finished_at": self.clock(),
            },
        )
        (directory / "request.json").unlink(missing_ok=True)

    async def _run(
        self,
        config: AppConfig,
        source_id: str,
        settings: dict[str, Any],
        state: dict[str, Any],
        lease: SourceOperationLease,
        collector: SourceOperationLease,
    ) -> None:
        if settings.get("mode") == "full_then_incremental":
            await self._run_history(config, source_id, settings, state, lease, collector)
            return
        directory = _directory(config, source_id)
        service: SourceImportService | None = None
        close: Callable[[], Awaitable[None]] | None = None
        work: asyncio.Task[Any] | None = None
        importing = False
        publication_completed = False
        identity: str | None = None
        stop_code: str | None = None

        def save(**extra: Any) -> None:
            state.update(extra)
            state["heartbeat_at"] = self.clock()
            _atomic_json(directory / "status.json", state)

        def check() -> str | None:
            if self._closing:
                return "cancelled"
            if _read(directory / "cancel.json").get("job_id") == state["job_id"]:
                return "cancelled"
            try:
                if _config_signature(self.config_provider(), source_id) != identity:
                    return "config_changed"
            except (ValueError, OSError):
                return "config_changed"
            return None

        async def wait_for_work() -> Any:
            nonlocal stop_code
            assert work is not None
            while not work.done():
                stop_code = stop_code or check()
                if stop_code:
                    save(state="stopping", message="正在安全停止本次任务。")
                    if importing:
                        assert service is not None
                        service.cancel(source_id)
                    else:
                        work.cancel()
                if importing and service is not None:
                    progress = service.status(source_id) or {}
                    if progress.get("state") == "committing":
                        save(phase="committing", state="stopping" if stop_code else "committing")
                save()
                await asyncio.wait({work}, timeout=0.25)
            return await work

        try:
            identity = _config_signature(config, source_id)
            # 先验证数据库可用；绝不以文件缓存或 MemoryStore 替代真实发布。
            service, close = await self.service_factory(config)
            try:
                await service.store.info()
            except Exception:
                raise SourceCollectError("database_unavailable", status=503) from None
            await _blocking(self._verify_ownership, service, source_id, settings["uid"])
            if check():
                raise SourceCollectError(check() or "cancelled")
            checkpoint = _read(directory / "checkpoint.json")
            if checkpoint.get("signature") != _signature(settings):
                checkpoint = {}
            fetcher = self.fetcher
            if fetcher is None:
                from jianshanagent.weibo_collect import fetch_weibo

                fetcher = fetch_weibo

            def progress(value: dict[str, Any]) -> None:
                clean = {
                    key: value[key]
                    for key in ("pages", "fetched", "accepted", "limited")
                    if key in value and isinstance(value[key], (int, bool))
                }
                save(**clean)

            async def fetch() -> Any:
                return await fetcher(
                    settings,
                    WeiboCredentialStore(config.root),
                    checkpoint=checkpoint.get("checkpoint"),
                    progress=progress,
                )

            work = asyncio.create_task(fetch())
            batch = await wait_for_work()
            stop_code = stop_code or check()
            if stop_code:
                raise SourceCollectError(stop_code)
            save(
                state="importing",
                phase="importing",
                pages=batch.pages,
                fetched=batch.fetched,
                accepted=len(batch.rows),
                limited=batch.limited,
                message="正在增量导入微博资料。",
            )
            content = b"".join(
                (
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    + "\n"
                ).encode("utf-8")
                for row in batch.rows
            )
            importing = True
            work = asyncio.create_task(service.import_data(source_id, content))
            result = await wait_for_work()
            if result.get("state") != "completed":
                raise SourceCollectError(stop_code or "failed")
            publication_completed = True
            state.update(
                last_success_at=self.clock(),
                new=result.get("new", 0),
                updated=result.get("updated", 0),
                unchanged=result.get("unchanged", 0),
            )
            # 提交若已经越过取消边界并安全完成，应报告实际成功，不能伪报已取消。
            # 但配置改变/取消仍不推进采集游标，下一轮可幂等重扫。
            if not stop_code and not check():
                _atomic_json(
                    directory / "checkpoint.json",
                    {
                        "signature": _signature(settings),
                        "checkpoint": batch.checkpoint,
                    },
                )
            save(
                state="completed",
                phase="completed",
                finished_at=self.clock(),
                last_success_at=self.clock(),
                new=result.get("new", 0),
                updated=result.get("updated", 0),
                unchanged=result.get("unchanged", 0),
                message="本批资料已成功发布。",
                error_code=None,
            )
        except asyncio.CancelledError:
            if work is not None and not work.done():
                if importing and service is not None:
                    service.cancel(source_id)
                else:
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            code = stop_code or "cancelled"
            final = "failed" if code == "config_changed" else "cancelled"
            if service is not None and importing:
                imported = service.status(source_id) or {}
                if imported.get("state") == "recovery_required":
                    final = code = "recovery_required"
            save(
                state=final,
                phase=final,
                finished_at=self.clock(),
                error_code=code,
                message=_MESSAGES.get(code, _MESSAGES["failed"]),
            )
        except Exception as exc:
            from jianshanagent.weibo_collect import WeiboCollectError

            if isinstance(exc, (SourceCollectError, WeiboCollectError)):
                code, message = exc.code, exc.public_message
            elif isinstance(exc, SourceImportCapacityError):
                code, message = f"capacity_{exc.code}", exc.public_message
            else:
                code, message = (
                    stop_code or "failed",
                    _MESSAGES.get(stop_code or "failed", _MESSAGES["failed"]),
                )
            final = (
                "cancelled"
                if code == "cancelled"
                else "authentication_required"
                if code == "authentication_required"
                else "failed"
            )
            if service is not None and importing:
                try:
                    imported = service.status(source_id) or {}
                except (ValueError, OSError):
                    imported = {"state": "uncertain"}
                if imported.get("state") == "recovery_required":
                    final, code, message = (
                        "recovery_required",
                        "recovery_required",
                        "提交恢复未完成，请重试恢复；断点未推进。",
                    )
                elif (
                    publication_completed
                    or getattr(exc, "published", False)
                    or imported.get("state") in {"completed", "uncertain"}
                    or imported.get("published")
                ):
                    final, code, message = (
                        "failed",
                        "publication_uncertain",
                        _MESSAGES["publication_uncertain"],
                    )
            save(
                state=final, phase=final, finished_at=self.clock(), error_code=code, message=message
            )
        finally:
            if work is not None and not work.done():
                if importing and service is not None:
                    service.cancel(source_id)
                else:
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            try:
                if service is not None:
                    await service.aclose()
                if close is not None:
                    with contextlib.suppress(Exception):
                        await close()
                # 取消/失败不立即重试风暴；定时下一周期重试，手动请求随时可重试。
                save(
                    next_run_at=self.clock() + settings["interval_seconds"]
                    if settings["enabled"]
                    else None
                )
            finally:
                collector.close()
                lease.close()

    async def _run_history(
        self,
        config: AppConfig,
        source_id: str,
        settings: dict[str, Any],
        state: dict[str, Any],
        lease: SourceOperationLease,
        collector: SourceOperationLease,
    ) -> None:
        from jianshanagent.plugins.knowledge.settings import kb_root
        from jianshanagent.weibo_client import RequestBudgetReached, WeiboCollectError
        from jianshanagent.weibo_history import (
            FREE_FLOOR,
            MAX_BATCH_BYTES,
            fetch_history_page,
            measure_resources,
        )
        from jianshanagent.weibo_history_queue import HistoryQueue

        directory = _directory(config, source_id)
        queue: HistoryQueue | None = None
        service: SourceImportService | None = None
        close: Callable[[], Awaitable[None]] | None = None
        work: asyncio.Task[Any] | None = None
        import_source: str | None = None
        stop_code: str | None = None
        identity = _config_signature(config, source_id)
        batch_started = time.monotonic()
        resources: dict[str, Any] = {}

        def save(**extra: Any) -> None:
            if queue is not None:
                state.update(queue.status())
            state.update({key: value for key, value in resources.items() if key != "blocked"})
            state.update(extra)
            state["heartbeat_at"] = self.clock()
            _atomic_json(directory / "status.json", state)

        def check() -> str | None:
            if self._closing or _read(directory / "cancel.json").get("job_id") == state["job_id"]:
                return "cancelled"
            if _read(directory / "pause.json").get("paused") is True:
                return "cancelled"
            try:
                if _config_signature(self.config_provider(), source_id) != identity:
                    return "config_changed"
            except (ValueError, OSError):
                return "config_changed"
            return None

        async def wait_work() -> Any:
            nonlocal stop_code
            assert work is not None
            while not work.done():
                stop_code = stop_code or check()
                if stop_code:
                    save(state="stopping", phase="stopping")
                    if import_source and service is not None:
                        service.cancel(import_source)
                    else:
                        work.cancel()
                if import_source and service is not None:
                    progress = service.status(import_source) or {}
                    if progress.get("state") == "committing":
                        save(state="stopping" if stop_code else "committing", phase="committing")
                await asyncio.wait({work}, timeout=0.25)
            return await work

        async def measure() -> dict[str, Any]:
            assert queue is not None and service is not None
            members = [source_id]
            if settings.get("discussion_source_id"):
                members.append(settings["discussion_source_id"])
            from jianshanagent.plugins.knowledge.source_import_v2 import measure_stream_resources

            # Shared API/CLI/worker ledger also validates the physical-volume proof.
            usage = await measure_stream_resources(
                config, service.store, source_id, resource_probe=service.resource_probe
            )
            pg_growth = int(usage["pg_growth_bytes"])
            if int(usage["pg_disk_free_bytes"]) < FREE_FLOOR + 2 * MAX_BATCH_BYTES:
                raise SourceCollectError("resource_limit")
            return await _blocking(
                partial(
                    measure_resources,
                    queue,
                    [kb_root(config) / member for member in members],
                    pg_bytes=pg_growth,
                )
            )

        try:
            queue = HistoryQueue(
                directory / "history.sqlite3",
                settings["uid"],
                _signature(settings),
                now=self.clock(),
            )
            if state.get("manual"):
                queue.pause(False)
            if queue.get("paused"):
                save(state="paused", phase="paused", next_run_at=None, next_batch_at=None)
                return
            service, close = await self.service_factory(config)
            try:
                await service.store.info()
            except Exception:
                raise SourceCollectError("database_unavailable", status=503) from None
            if not queue.get("ownership_verified"):
                await _blocking(self._verify_ownership, service, source_id, settings["uid"])
                with queue.transaction():
                    queue.set("ownership_verified", True)
            if check():
                raise SourceCollectError(check() or "cancelled")
            resources = await measure()
            if resources["blocked"]:
                raise SourceCollectError("resource_limit")
            save(state="fetching", phase="fetching")
            work = asyncio.create_task(
                fetch_history_page(queue, settings, WeiboCredentialStore(config.root))
            )
            pending = await wait_work()
            if stop_code or check():
                raise SourceCollectError(stop_code or check() or "cancelled")
            if pending is not None:
                published = 0
                for lane, rows in queue.publications():
                    if published and (time.monotonic() - batch_started >= 180 or published >= 100):
                        save(
                            state="completed",
                            phase="completed",
                            limited=True,
                            finished_at=self.clock(),
                            error_code=None,
                        )
                        return
                    target = source_id if lane == "posts" else settings.get("discussion_source_id")
                    if not target:
                        raise SourceCollectError("invalid_source")
                    content = b"".join(
                        (
                            json.dumps(
                                row, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                            )
                            + "\n"
                        ).encode("utf-8")
                        for row in rows
                    )
                    save(
                        state="importing",
                        phase="importing",
                        history_phase="indexing",
                        accepted=len(rows),
                    )
                    import_source = target
                    work = asyncio.create_task(service.import_data(target, content))
                    result = await wait_work()
                    if result.get("state") != "completed":
                        raise SourceCollectError(stop_code or "failed")
                    state.update(
                        new=result.get("new", 0),
                        updated=result.get("updated", 0),
                        unchanged=result.get("unchanged", 0),
                    )
                    import_source = None
                    if stop_code or check():
                        raise SourceCollectError(stop_code or check() or "cancelled")
                    queue.publication_succeeded()
                    published += 1
                # Empty terminal pages require no index publication, but their proposed
                # cursor still receives the same cancellation/config checks above.
                queue.acknowledge(now=self.clock())
                save(last_success_at=self.clock())
            resources = await measure()
            if resources["blocked"]:
                raise SourceCollectError("resource_limit")
            if queue.get("history_phase") == "verifying" and not queue.verify():
                raise SourceCollectError("count_mismatch")
            measurement = queue.get("measurement", {})
            measurement["batch_seconds"] = round(time.monotonic() - batch_started, 3)
            with queue.transaction():
                queue.set("measurement", measurement)
            save(
                state="completed",
                phase="completed",
                error_code=None,
                finished_at=self.clock(),
                message="本批资料已成功发布，历史队列按真实水位继续。",
            )
        except (RequestBudgetReached, TimeoutError):
            # A page is retried in its entirety; no cursor or node packet is committed halfway.
            save(
                state="completed",
                phase="completed",
                limited=True,
                finished_at=self.clock(),
                error_code=None,
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                code, message = stop_code or "cancelled", _MESSAGES[stop_code or "cancelled"]
            elif isinstance(exc, (SourceCollectError, WeiboCollectError)):
                code, message = exc.code, exc.public_message
            elif isinstance(exc, SourceImportCapacityError):
                code, message = f"capacity_{exc.code}", exc.public_message
            elif isinstance(exc, Exception):
                code, message = "failed", _MESSAGES["failed"]
            else:
                raise
            if work is not None and not work.done():
                if import_source and service is not None:
                    service.cancel(import_source)
                else:
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            if queue is not None:
                queue.pause(True, reason=None if code == "cancelled" else code)
            _atomic_json(directory / "pause.json", {"paused": True})
            save(
                state="paused" if code == "cancelled" else "blocked",
                phase="paused" if code == "cancelled" else "blocked",
                history_phase="paused" if code == "cancelled" else "blocked",
                paused=True,
                error_code=code,
                message=message,
                finished_at=self.clock(),
                next_run_at=None,
                next_batch_at=None,
            )
        finally:
            try:
                if service is not None:
                    await service.aclose()
                if close is not None:
                    with contextlib.suppress(Exception):
                        await close()
                paused = queue is not None and queue.get("paused")
                if queue is not None and not paused:
                    pending = queue.status()
                    unfinished = any(
                        pending[key]
                        for key in (
                            "pending_posts",
                            "pending_comments",
                            "pending_replies",
                            "index_pending",
                        )
                    )
                    delay = (
                        2.0
                        if not queue.get("history_complete") or unfinished
                        else settings["interval_seconds"]
                    )
                    next_at = (
                        self.clock() + delay
                        if settings["enabled"] or not queue.get("history_complete")
                        else None
                    )
                    save(next_run_at=next_at, next_batch_at=next_at)
            finally:
                if queue is not None:
                    queue.close()
                collector.close()
                lease.close()

    @staticmethod
    def _verify_ownership(service: SourceImportService, source_id: str, uid: str) -> None:
        from jianshanagent.plugins.knowledge.source_import_v2 import iter_source_rows

        for row in iter_source_rows(service.config, source_id):
            if not isinstance(row, dict) or row.get("author_id") != uid:
                raise SourceCollectError("ownership_unverified")

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except (ValueError, OSError):
                    # 无效磁盘配置不能拖垮 Wiki 健康；下一 ticker 重新读取。
                    pass
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), POLL_SECONDS)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        self._closing = True
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
