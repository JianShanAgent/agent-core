"""插件合不合规范的自检。每个插件仓库的测试里调一次::

    from jianshanagent.testing.conformance import check_plugin

    def test_conforms_to_the_plugin_spec() -> None:
        check_plugin("jianshanagent_site_huiji_yys")

查的是 ``docs/插件规范.md`` 里能用程序查的那部分：登记了没有、名字照没照规矩取、清单读不读得出来、
清单里说的能力包里有没有、默认配置是不是合法的 YAML。
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import yaml

from jianshanagent.core import manifest as manifest_module
from jianshanagent.core.manifest import (
    CAPABILITY_EXTEND,
    CONFIG_FILE,
    ENTRY_POINTS,
    KIND_FEATURE,
    KIND_SITE,
    Manifest,
    PluginError,
    feature_manifests,
    installed_sites,
    site_chain,
)

#: 各种插件的包名前缀
PACKAGE_PREFIX = {KIND_FEATURE: "jianshanagent_plugin_", KIND_SITE: "jianshanagent_site_"}


def expected_package(manifest: Manifest, chain: tuple[Manifest, ...] = ()) -> str:
    """照命名规范，这个插件的包该叫什么：前缀 + 从最上面一级到它自己的 id，用下划线连起来。"""
    ids = [m.id for m in chain] if chain else [manifest.id]
    return PACKAGE_PREFIX[manifest.kind] + "_".join(ids)


def check_plugin(package: str) -> Manifest:
    """``package`` 是不是一个合规范的、登记了的插件。

    不合规范抛 :class:`PluginError`，说明哪里不对。
    """
    kind = next((k for k, prefix in PACKAGE_PREFIX.items() if package.startswith(prefix)), "")
    if not kind:
        raise PluginError(
            f"包名「{package}」不照规范：功能插件叫 {PACKAGE_PREFIX[KIND_FEATURE]}<id>，"
            f"站点插件叫 {PACKAGE_PREFIX[KIND_SITE]}<平台>[_<站>]"
        )
    # 每次现问（测试会换掉登记表）
    entries = manifest_module._entry_points(ENTRY_POINTS[kind])
    registered = {value: name for name, value in entries}
    if package not in registered:
        raise PluginError(
            f"「{package}」没有登记：在 pyproject.toml 的 "
            f'[project.entry-points."{ENTRY_POINTS[kind]}"] 里写 <id> = "{package}"，再重新安装'
        )
    plugin_id = registered[package]
    if kind == KIND_FEATURE:
        manifest = feature_manifests()[plugin_id]
        chain: tuple[Manifest, ...] = ()
    else:
        manifest = installed_sites()[plugin_id]
        chain = site_chain(plugin_id)  # 上级找不到、绕成圈，在这里报
    wanted = expected_package(manifest, chain)
    if package != wanted:
        raise PluginError(f"插件「{plugin_id}」的包叫「{package}」，照命名规范应该叫「{wanted}」")
    if not manifest.description:
        raise PluginError(f"{manifest.path} 的清单没有写 description（一两句话说明它管什么）")

    config = manifest.path / CONFIG_FILE
    if config.is_file():
        content = yaml.safe_load(config.read_text(encoding="utf-8"))
        if content is not None and not isinstance(content, dict):
            raise PluginError(f"{config} 要写成键值结构")
        if kind == KIND_SITE and (content or {}).get(CAPABILITY_EXTEND):
            if CAPABILITY_EXTEND not in manifest.capabilities:
                raise PluginError(f"{config} 里有 extend 段，清单的 capabilities 里却没有写 extend")

    if kind == KIND_FEATURE:
        module = importlib.import_module(package)
        if not callable(getattr(module, "create_plugin", None)):
            raise PluginError(f"功能插件「{plugin_id}」的包里没有 create_plugin()")
    else:
        for capability in manifest.capabilities:
            if capability == CAPABILITY_EXTEND:
                continue
            if importlib.util.find_spec(f"{package}.{capability}") is None:
                raise PluginError(
                    f"站点插件「{plugin_id}」的清单里写了能力 {capability}，"
                    f"包里却没有 {capability} 模块"
                )
            module = importlib.import_module(f"{package}.{capability}")
            if not callable(getattr(module, "create", None)):
                raise PluginError(
                    f"站点插件「{plugin_id}」的 {capability} 模块里没有 create(setup)"
                )
    for required in ("README.md", "pyproject.toml"):
        root = _repo_root(manifest.path)
        if root is not None and not (root / required).is_file():
            raise PluginError(f"插件「{plugin_id}」的仓库里没有 {required}")
    return manifest


def _repo_root(package_dir: Path) -> Path | None:
    """插件的包在 ``<仓库>/src/<包>`` 时返回仓库目录；装成普通的包（不在源码目录里）时返回 None。"""
    return package_dir.parents[1] if package_dir.parent.name == "src" else None
