"""测试用的「一个部署的配置目录」：纯 MediaWiki，不带站点插件。

真的部署目录里只放和默认值不一样的地方（``tests/config`` 就是这样）。有些测试要整份地拷走、
按文件改（模拟「这个部署改了某项」），所以这里另备一份摊开的：包里的默认配置都在，
``tests/config`` 里写的并在上面。两份读出来的生效配置是一样的。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import yaml

from jianshanagent.core.config import DEFAULTS_DIR, _deep_merge

#: 只写了改动的那一份
CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _spread() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jianshanagent-test-config-"))
    for path in DEFAULTS_DIR.glob("*.yaml"):
        shutil.copy(path, root / path.name)
    for path in CONFIG_DIR.glob("*.yaml"):
        target = root / path.name
        if target.exists() and not path.name.startswith("secrets."):
            merged = _deep_merge(
                yaml.safe_load(target.read_text("utf-8")) or {},
                yaml.safe_load(path.read_text("utf-8")) or {},
            )
            target.write_text(yaml.safe_dump(merged, allow_unicode=True), encoding="utf-8")
        else:
            shutil.copy(path, target)
    return root


#: 摊开的那一份（每次跑测试现做，在临时目录里）
FULL_CONFIG_DIR = _spread()
