"""日志初始化：第三方库的日志哪些要、哪些不要。"""

import logging
from collections.abc import Iterator

import httpx
import pytest
import structlog

from jianshanagent.core.logging import LOG_BUFFER, setup_logging

_THIRD_PARTY = ("httpx", "httpcore")


@pytest.fixture
def logging_state() -> Iterator[None]:
    """setup_logging 改的是全进程的日志设置：测完恢复原样，不影响别的测试。"""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in _THIRD_PARTY}
    config = structlog.get_config()
    yield
    structlog.configure(**config)
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, kept in levels.items():
        logging.getLogger(name).setLevel(kept)


async def test_request_urls_from_httpx_stay_out_of_the_log(logging_state: None) -> None:
    root = logging.getLogger()
    setup_logging(level="INFO", json_logs=False)
    root.setLevel(logging.INFO)  # 线上就是这样；测试框架先占了根日志器，setup_logging 设不上
    # httpx 在 INFO 级别每发一个请求打一行完整地址：用户图片的下载地址（带访问凭据）、
    # wiki 的搜索词（来自群里说的话）都会进日志和管理后台的「实时日志」
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    before = LOG_BUFFER.last
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await client.get("https://multimedia.test/download?fileid=F1&rkey=SECRET_RKEY")
    logging.getLogger("别的库").info("照常记")
    lines = [str(entry) for entry in LOG_BUFFER.recent(after=before)]
    assert not any("SECRET_RKEY" in line or "HTTP Request" in line for line in lines)
    assert any("照常记" in line for line in lines)  # 别的库的日志不受影响

    # 把整体级别调到 DEBUG（排查问题时）也不带出来；整体更严时跟着更严
    setup_logging(level="DEBUG", json_logs=False)
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    setup_logging(level="ERROR", json_logs=False)
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.ERROR
