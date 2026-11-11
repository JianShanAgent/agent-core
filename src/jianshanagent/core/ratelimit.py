"""进程内的滑动窗口限流。单进程部署够用；多进程时再换成 Redis 实现。"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self, *, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic
    ):
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._notified: dict[str, float] = {}

    def allow(self, key: str, limit: int) -> bool:
        """窗口内未超过 ``limit`` 次则记一次并放行；``limit <= 0`` 表示不限。"""
        if limit <= 0:
            return True
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def should_notify(self, key: str) -> bool:
        """被限流后是否该提示：每个窗口只提示一次，其余静默，避免提示本身刷屏。"""
        now = self._clock()
        last = self._notified.get(key)
        if last is not None and now - last < self._window:
            return False
        self._notified[key] = now
        return True
