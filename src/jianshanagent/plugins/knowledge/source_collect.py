"""文档来源共享操作 lease 与采集控制；API 只写控制记录，不执行网络或导入。"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jianshanagent.core.config import AppConfig
from jianshanagent.core.embedding import EmbeddingSettings
from jianshanagent.core.sources import source_instance
from jianshanagent.plugins.knowledge.settings import kb_root
from jianshanagent.plugins.knowledge.source_import import _atomic_json, _read_json

ACTIVE_STATES = frozenset({"queued", "fetching", "importing", "committing", "stopping"})
STATUS_FIELDS = frozenset(
    {
        "state",
        "phase",
        "job_id",
        "uid",
        "profile_url",
        "pages",
        "fetched",
        "accepted",
        "new",
        "updated",
        "unchanged",
        "started_at",
        "finished_at",
        "last_success_at",
        "next_run_at",
        "error_code",
        "message",
        "limited",
        "heartbeat_at",
        "source_id",
    }
)
_MESSAGES = {
    "busy": "该来源已有操作或采集请求，请稍后重试。",
    "invalid_source": "该来源不是已配置采集目标的微博来源。",
    "disabled": "来源已停用，不能开始采集。",
    "stale_job": "任务已变化，请刷新状态后重试。",
    "unsafe_storage": "来源控制记录不可用，请联系管理员。",
    "interrupted": "上次采集已中断，请刷新状态核实结果，并从已保存断点重试。",
    "publication_uncertain": "资料已发布或结果待确认，请刷新状态；下次将从已保存断点幂等复查。",
    "config_changed": "来源或采集配置已变化，本次任务已停止。",
    "database_unavailable": "数据库不可用，未发布资料，断点未推进。",
    "failed": "采集失败，旧资料与断点保持不变。",
    "cancelled": "采集已取消，断点未推进。",
    "ownership_unverified": "已有资料无法确认属于目标账号，拒绝混合历史。",
}


class SourceCollectError(ValueError):
    def __init__(self, code: str, *, status: int = 409):
        self.code = code if code in _MESSAGES else "failed"
        self.public_message = _MESSAGES[self.code]
        self.status = status
        super().__init__(self.public_message)


class SourceOperationLease:
    """构造时已持锁；close 幂等，不能把未进入的 contextmanager 当成功拿锁。"""

    def __init__(self, fd: int):
        self.fd: int | None = fd

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self) -> SourceOperationLease:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _directory(config: AppConfig, source_id: str, *, operation: bool = False) -> Path:
    try:
        source = source_instance(config, source_id)
        if source.driver != "documents" or not re.fullmatch(r"[a-z][a-z0-9_]*", source_id):
            raise ValueError()
        root = kb_root(config)
        directory = root / source_id / ("source-import" if operation else "source-collect")
        if any(part.is_symlink() for part in (root, *root.parents, root / source_id, directory)):
            raise SourceCollectError("unsafe_storage", status=503)
        return directory
    except SourceCollectError:
        raise
    except (ValueError, OSError):
        raise SourceCollectError("invalid_source", status=400) from None


def _acquire(path: Path) -> SourceOperationLease:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SourceCollectError("unsafe_storage", status=503)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return SourceOperationLease(fd)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EACCES}:
            raise SourceCollectError("busy") from None
        raise SourceCollectError("unsafe_storage", status=503) from None


def acquire_source_operation(config: AppConfig, source_id: str) -> SourceOperationLease:
    return _acquire(_directory(config, source_id, operation=True) / "operation.lock")


def _held(path: Path) -> bool:
    if not path.exists():
        return False
    lease: SourceOperationLease | None = None
    try:
        lease = _acquire(path)
        return False
    except SourceCollectError as exc:
        if exc.code == "busy":
            return True
        raise
    finally:
        if lease is not None:
            lease.close()


@contextmanager
def _control(config: AppConfig, source_id: str) -> Iterator[Path]:
    directory = _directory(config, source_id)
    # 仅保护短小的本地原子读写；worker 不持这个锁等 HTTP/PG。
    path = directory / "control.lock"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except OSError:
        raise SourceCollectError("unsafe_storage", status=503) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceCollectError("unsafe_storage", status=503)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = _read_json(path)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, OSError, TypeError):
        raise SourceCollectError("unsafe_storage", status=503) from None


def _settings(config: AppConfig, source_id: str, *, mounted: bool = False) -> dict[str, Any]:
    from jianshanagent.weibo_collector_config import parse_weibo_collector

    try:
        source = source_instance(config, source_id)
        if source.platform.id != "weibo" or source.driver != "documents":
            raise ValueError()
        settings = parse_weibo_collector(source.raw.get("collector"))
        if not isinstance(settings, dict):
            raise ValueError()
        if mounted and not source.mounted:
            raise SourceCollectError("disabled")
        return settings
    except SourceCollectError:
        raise
    except (ValueError, TypeError):
        raise SourceCollectError("invalid_source", status=400) from None


def _signature(settings: dict[str, Any]) -> str:
    # enabled/interval 只影响调度，不改变已采集数据的归属或窗口。
    data = {k: v for k, v in settings.items() if k not in {"enabled", "interval_seconds"}}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _config_signature(config: AppConfig, source_id: str) -> str:
    source = source_instance(config, source_id)
    embedding = EmbeddingSettings.from_config(config)
    identity = [
        source.mounted,
        source.role,
        source.platform.id,
        _signature(_settings(config, source_id)),
        str(kb_root(config)),
        embedding.provider().fingerprint if embedding else None,
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _public(value: dict[str, Any]) -> dict[str, Any]:
    """控制文件不是可公开日志；只重新构造固定元数据，不透传自由字符串。"""
    states = {
        "not_configured": "请先配置微博目标账号。",
        "paused": "定时采集已关闭。",
        "idle": "等待采集。",
        "queued": "采集请求已排队，等待同步进程。",
        "fetching": "正在采集公开微博。",
        "importing": "正在增量导入微博资料。",
        "committing": "正在安全发布资料。",
        "stopping": "正在安全停止本次任务。",
        "completed": "本批资料已成功发布。",
        "failed": _MESSAGES["failed"],
        "cancelled": _MESSAGES["cancelled"],
        "interrupted": _MESSAGES["interrupted"],
        "authentication_required": "微博会话已失效，请重新同步登录会话。",
        "recovery_required": "提交恢复未完成，请重试恢复；断点未推进。",
    }
    state = value.get("state")
    state = state if isinstance(state, str) and state in states else "failed"
    result: dict[str, Any] = {
        "state": state,
        "phase": state,
        "job_id": None,
        "uid": None,
        "profile_url": None,
        "pages": 0,
        "fetched": 0,
        "accepted": 0,
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "started_at": None,
        "finished_at": None,
        "last_success_at": None,
        "next_run_at": None,
        "error_code": None,
        "message": states[state],
        "limited": value.get("limited") is True,
    }
    if isinstance(value.get("phase"), str) and value["phase"] in states:
        result["phase"] = value["phase"]
    for field in ("pages", "fetched", "accepted", "new", "updated", "unchanged"):
        number = value.get(field)
        if type(number) is int and 0 <= number <= 10**9:
            result[field] = number
    for field in ("started_at", "finished_at", "last_success_at", "next_run_at", "heartbeat_at"):
        moment = value.get(field)
        if (
            isinstance(moment, (int, float))
            and not isinstance(moment, bool)
            and math.isfinite(moment)
            and 0 <= moment <= 253402300799
        ):
            result[field] = moment
    for field, pattern in (
        ("uid", r"[1-9][0-9]{0,31}"),
        ("job_id", r"[0-9a-f]{32}"),
        ("source_id", r"[a-z][a-z0-9_]*"),
    ):
        text = value.get(field)
        if isinstance(text, str) and re.fullmatch(pattern, text):
            result[field] = text
    if result["uid"]:
        result["profile_url"] = f"https://weibo.com/u/{result['uid']}"
    code = value.get("error_code")
    if isinstance(code, str) and code:
        from jianshanagent.plugins.knowledge.source_import import (
            CAPACITY_ERROR_CODES,
            CAPACITY_PUBLIC_MESSAGE,
        )

        transport_codes = {
            "authentication_required",
            "risk_control",
            "rate_limited",
            "network",
            "response_too_large",
            "schema_changed",
            "author_mismatch",
            "pagination_stalled",
            "invalid_checkpoint",
            "invalid_settings",
            "detail_unavailable",
            "row_too_large",
        }
        if code in _MESSAGES:
            result.update(error_code=code, message=_MESSAGES[code])
        elif code == "recovery_required":
            result.update(error_code=code, message=states[code])
        elif code.startswith("capacity_") and code[9:] in CAPACITY_ERROR_CODES:
            result.update(error_code=code, message=CAPACITY_PUBLIC_MESSAGE)
        elif code in transport_codes:
            from jianshanagent.weibo_collect import WeiboCollectError

            result.update(error_code=code, message=WeiboCollectError(code).public_message)
        else:
            result.update(error_code="failed", message=_MESSAGES["failed"])
    return result


def collector_overview(config: AppConfig, source_id: str) -> dict[str, Any]:
    directory = _directory(config, source_id)
    state = _read(directory / "status.json")
    request = _read(directory / "request.json")
    if request and request.get("job_id") != state.get("job_id"):
        state = request
    if not state:
        state = {"state": "idle", "phase": "idle", "source_id": source_id}
    state = _public(state)
    if state.get("state") in ACTIVE_STATES - {"queued"} and not _held(directory / "collector.lock"):
        state = {
            **state,
            "state": "interrupted",
            "phase": "interrupted",
            "error_code": "interrupted",
            "message": _MESSAGES["interrupted"],
        }
    cancel = _read(directory / "cancel.json")
    if cancel.get("job_id") == state.get("job_id") and state.get("state") in ACTIVE_STATES:
        state = {**state, "state": "stopping"}
    try:
        settings = _settings(config, source_id)
        if not state.get("uid"):
            state["uid"] = settings["uid"]
        state["profile_url"] = f"https://weibo.com/u/{state['uid']}"
        if not settings["enabled"] or not source_instance(config, source_id).mounted:
            state["next_run_at"] = None
            if state["state"] == "idle":
                state.update(state="paused", phase="paused")
    except SourceCollectError:
        state["next_run_at"] = None
        if state["state"] == "idle":
            state.update(state="not_configured", phase="not_configured")
    return _public(state)


def collector_active(config: AppConfig, source_id: str) -> bool:
    return collector_overview(config, source_id).get("state") in ACTIVE_STATES or _held(
        _directory(config, source_id, operation=True) / "operation.lock"
    )


def request_collect(config: AppConfig, source_id: str) -> dict[str, Any]:
    settings = _settings(config, source_id, mounted=True)
    with acquire_source_operation(config, source_id), _control(config, source_id) as directory:
        if collector_overview(config, source_id).get("state") in ACTIVE_STATES:
            raise SourceCollectError("busy")
        old = _read(directory / "status.json")
        value = {
            "source_id": source_id,
            "job_id": uuid.uuid4().hex,
            "state": "queued",
            "phase": "queued",
            "uid": settings["uid"],
            "profile_url": f"https://weibo.com/u/{settings['uid']}",
            "requested_at": time.time(),
            "signature": _signature(settings),
            "config_signature": _config_signature(config, source_id),
            "pages": 0,
            "fetched": 0,
            "accepted": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "started_at": None,
            "finished_at": None,
            "last_success_at": old.get("last_success_at"),
            "next_run_at": None,
            "error_code": None,
            "message": "采集请求已排队，等待同步进程。",
            "limited": False,
        }
        _atomic_json(directory / "request.json", value)
        _atomic_json(directory / "status.json", value)
        return _public(value)


def cancel_collect(config: AppConfig, source_id: str, job_id: str) -> dict[str, Any]:
    with _control(config, source_id) as directory:
        state = collector_overview(config, source_id)
        if not isinstance(job_id, str) or not job_id or state.get("job_id") != job_id:
            raise SourceCollectError("stale_job")
        if state.get("state") not in ACTIVE_STATES:
            return state
        _atomic_json(directory / "cancel.json", {"job_id": job_id})
        if state["state"] == "queued":
            try:
                settings = _settings(config, source_id)
            except SourceCollectError:
                settings = {"enabled": False}
            state = {
                **_read(directory / "status.json"),
                "schedule_signature": json.dumps(settings, sort_keys=True),
                "next_run_at": (
                    time.time() + settings["interval_seconds"] if settings["enabled"] else None
                ),
                "state": "cancelled",
                "phase": "cancelled",
                "finished_at": time.time(),
                "error_code": "cancelled",
                "message": _MESSAGES["cancelled"],
            }
            _atomic_json(directory / "status.json", state)
            (directory / "request.json").unlink(missing_ok=True)
        else:
            state = {**state, "state": "stopping", "message": "正在安全停止本次任务。"}
        return _public(state)
