from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from typing import Any

from jianshanagent.runtime import Runtime


async def run() -> bool:
    """跑到被要求停下为止。返回要不要原地重新启动。"""
    runtime = Runtime()
    await runtime.start()

    def _ask_stop(*_args: Any) -> None:
        runtime.request_stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _ask_stop)

    try:
        await runtime.wait()
    finally:
        await runtime.stop()
    return runtime.restart_requested


def main() -> int:
    try:
        restart = asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    if restart:
        # 管理后台里点了「重启」：用同样的参数把自己重新执行一遍。进程号不变，
        # 所以容器不会跟着重建，日志也接得上
        os.execv(sys.executable, [sys.executable, "-m", "jianshanagent", *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
