from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

import structlog

_RESERVED = ("event", "level", "timestamp")


class LogBuffer:
    """最近的日志在内存里留一份，管理后台的「实时日志」看的就是它。

    只是为了在网页上随手看：进程重启就清空。更早的日志不归机器人管：它只往标准输出打，
    由部署插件去收（自带的 ``deploy/plugins/logs`` 是 Loki + Grafana）。
    日志可能从别的线程打出来（QQ 的连接跑在 SDK 自己的线程里），所以订阅的一方通过事件循环转交。
    """

    def __init__(self, capacity: int = 5000):
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: dict[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop] = {}

    def add(self, level: str, event: str, fields: dict[str, Any], *, time: str = "") -> None:
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "time": time or datetime.now(UTC).isoformat(),
                "level": level.lower(),
                "event": event,
                "fields": fields,
            }
            self._entries.append(entry)
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, entry)
            except RuntimeError:  # 那个事件循环已经关了
                self.unsubscribe(queue)

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, Any]], entry: dict[str, Any]) -> None:
        if queue.full():  # 看的人跟不上：丢最旧的，不能让日志在内存里越积越多
            queue.get_nowait()
        queue.put_nowait(entry)

    @property
    def last(self) -> int:
        """最新一条日志的序号（还没有日志是 0）。"""
        return self._seq

    def recent(self, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """序号大于 ``after`` 的日志里最新的 ``limit`` 条，从旧到新。"""
        with self._lock:
            entries = [entry for entry in self._entries if entry["seq"] > after]
        return entries[-limit:]

    def subscribe(self, maxsize: int = 2000) -> asyncio.Queue[dict[str, Any]]:
        """之后新打出来的日志都会放进返回的队列（要在事件循环里调用）。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers[queue] = asyncio.get_running_loop()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)


#: 进程里唯一的一份
LOG_BUFFER = LogBuffer()


def _plain(fields: dict[str, Any]) -> dict[str, Any]:
    """日志里带的值不一定是 JSON 认识的类型：认不出来的转成文字。"""
    try:
        plain: dict[str, Any] = json.loads(json.dumps(fields, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {key: str(value) for key, value in fields.items()}
    return plain


def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog 的处理环节：把这条日志抄一份进 :data:`LOG_BUFFER`，原样往下传。"""
    fields = {key: value for key, value in event_dict.items() if key not in _RESERVED}
    LOG_BUFFER.add(
        str(event_dict.get("level") or "info"),
        str(event_dict.get("event") or ""),
        _plain(fields),
        time=str(event_dict.get("timestamp") or ""),
    )
    return event_dict


#: 这些第三方库的日志只留 WARNING 以上（原因见 setup_logging）
_QUIET_LOGGERS = ("httpx", "httpcore")


class _BufferHandler(logging.Handler):
    """标准库 logging 打的日志（第三方库的）也抄一份。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields: dict[str, Any] = {"logger": record.name}
            if record.exc_info:
                fields["exception"] = logging.Formatter().formatException(record.exc_info)
            LOG_BUFFER.add(record.levelname, record.getMessage(), fields)
        except Exception:  # 记日志的环节自己不能出错
            self.handleError(record)


def setup_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)
    root = logging.getLogger()
    if not any(isinstance(handler, _BufferHandler) for handler in root.handlers):
        root.addHandler(_BufferHandler(level=log_level))
    # httpx 在 INFO 级别每发一个请求打一行完整地址：用户图片的下载地址（参数里带访问凭据）、
    # wiki 的搜索词（来自用户说的话）都会跟着进日志。请求成没成各处自己会记，这一行不要
    for noisy in _QUIET_LOGGERS:
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _capture,
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
