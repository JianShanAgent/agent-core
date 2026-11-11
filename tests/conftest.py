"""核心的测试是站在「一个纯 MediaWiki 的部署」里跑的：没有另外指定配置目录时，用 tests/config。"""

from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parent / "config"


@pytest.fixture(autouse=True)
def plain_mediawiki_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(CONFIG_DIR))
