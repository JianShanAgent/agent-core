"""进程内的滑动窗口限流。单进程部署够用；多进程时再换成 Redis 实现。"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._notified: OrderedDict[str, float] = OrderedDict()

    def allow(self, key: str, limit: int) -> bool:
        if limit <= 0:
            return True
        now = self._clock()
        while self._hits and now - next(iter(self._hits.values()))[-1] >= self._window:
            self._hits.popitem(last=False)
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                return False  # 不踢掉有效名单，否则换一批身份就能重置限额
            hits = self._hits[key] = deque()
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        self._hits.move_to_end(key)
        return True

    def should_notify(self, key: str) -> bool:
        """被限流后是否该提示：每个窗口只提示一次，其余静默，避免提示本身刷屏。"""
        now = self._clock()
        while self._notified and now - next(iter(self._notified.values())) >= self._window:
            self._notified.popitem(last=False)
        if key in self._notified or len(self._notified) >= self._max_keys:
            return False
        self._notified[key] = now
        return True
