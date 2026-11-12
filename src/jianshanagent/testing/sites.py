"""测试用：在临时目录里搭站点插件——一个包，里面有清单、默认配置和接扩展点的模块；
以及把包登记成插件（真装的插件是在 ``pyproject.toml`` 的 entry points 里登记的）。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from jianshanagent.core import manifest as manifest_module
from jianshanagent.core.manifest import ENTRY_POINTS, KIND_FEATURE, KIND_SITE


def site_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, top: str) -> Path:
    """建一个空的顶层包 ``top`` 并让它能被导入；返回它的目录，站点插件往里面放。"""
    root = tmp_path / "pkg" / top
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "pkg"))
    for name in [m for m in sys.modules if m == top or m.startswith(f"{top}.")]:
        monkeypatch.delitem(sys.modules, name)
    importlib.invalidate_caches()
    return root


def one_wiki(
    site: str = "",
    *,
    wiki_id: str = "main",
    api_url: str = "https://wiki.example/api.php",
    **more: Any,
) -> dict[str, Any]:
    """``core.wiki`` 的内容：只挂一个 wiki，用 ``site`` 这个站点插件。

    ``more``：这个 wiki 的别的项（``site_base``、``article_path``、``label``……）。
    """
    return {"wikis": [{"id": wiki_id, "api_url": api_url, "site": site, **more}]}


def write_site(
    directory: Path,
    *,
    parent: str = "",
    capabilities: tuple[str, ...] = (),
    config: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    **modules: str,
) -> Path:
    """在 ``directory`` 搭一个站点插件（目录名就是它的 id）。

    ``config``：它的默认配置（``config.yaml``）；``modules``：接扩展点的模块，名字 → 源码；
    ``manifest``：要改写或加进清单里的项（测不合规范的清单时用）。
    下级插件放在上级目录的 ``sites/`` 里，id 以上级的 id 开头：
    ``write_site(上级目录 / "sites" / "上级_子站", parent="上级")``。
    """
    directory.mkdir(parents=True, exist_ok=True)
    for package in (directory, directory.parent if directory.parent.name == "sites" else None):
        if package is not None:
            (package / "__init__.py").write_text("", encoding="utf-8")
    content: dict[str, Any] = {
        "id": directory.name,
        "kind": "site",
        "name": f"测试站点 {directory.name}",
        "capabilities": list(capabilities),
    }
    if parent:
        content["parent"] = parent
    content |= manifest or {}
    (directory / "plugin.yaml").write_text(
        yaml.safe_dump(content, allow_unicode=True), encoding="utf-8"
    )
    if config is not None:
        (directory / "config.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
        )
    for module, source in modules.items():
        (directory / f"{module}.py").write_text(source, encoding="utf-8")
    importlib.invalidate_caches()
    return directory


def register_plugins(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sites: dict[str, str] | None = None,
    features: dict[str, str] | None = None,
    keep_installed: bool = False,
) -> None:
    """让核心以为这些包登记成了插件（id → 包路径），测试结束自动还原。

    ``keep_installed=False``（默认）：环境里真装着的插件当作没装，测试只看到这里给的；
    ``True``：在真装着的基础上再加。
    """
    real = manifest_module._entry_points
    fake = {
        ENTRY_POINTS[KIND_SITE]: sorted((sites or {}).items()),
        ENTRY_POINTS[KIND_FEATURE]: sorted((features or {}).items()),
    }

    def entry_points(group: str) -> list[tuple[str, str]]:
        return sorted([*(real(group) if keep_installed else []), *fake.get(group, [])])

    # monkeypatch 是后进先出地还原的：这一项最先登记，所以最后还原——那时登记表已经换回真的了，
    # 借它被删掉的那一下把缓存清掉，后面的测试不会看到这里给的假插件
    monkeypatch.setitem(_ClearOnRestore(), "registered", True)
    monkeypatch.setattr(manifest_module, "_entry_points", entry_points)
    manifest_module.clear_caches()


class _ClearOnRestore(dict[str, object]):
    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        manifest_module.clear_caches()
