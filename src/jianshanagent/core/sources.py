"""统一来源登记：兼容 Wiki 投影，文档来源不经过 WikiSite。"""

from __future__ import annotations

import importlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from jianshanagent.core.manifest import (
    DRIVER_DOCUMENTS,
    DRIVER_MEDIAWIKI,
    Manifest,
    PluginError,
    platforms,
)
from jianshanagent.weibo_collector_config import parse_weibo_collector

if TYPE_CHECKING:
    from jianshanagent.core.config import AppConfig
    from jianshanagent.plugins.knowledge.models import Document

SOURCE_FILE = "core.sources"
SOURCE_INSTANCES = "sources"
EXTENSION_DOCUMENTS = "documents"
_SOURCE_ID = re.compile(r"[a-z][a-z0-9_]*")
_SOURCE_KEYS = frozenset({"id", "label", "platform", "mounted", "role", "collector"})
SOURCE_ROLES = frozenset({"unknown", "official", "community"})


@dataclass(frozen=True, slots=True)
class SourceInstance:
    """来源 ID 同时是存储命名空间；角色只来自受控配置，不由导入记录决定。"""

    id: str
    label: str
    platform: Manifest
    driver: str
    mounted: bool
    role: str
    raw: dict[str, Any]

    @property
    def is_wiki(self) -> bool:
        return self.driver == DRIVER_MEDIAWIKI


@dataclass(frozen=True, slots=True)
class SourceSetup:
    config: AppConfig
    source: SourceInstance


class SourceAdapter(Protocol):
    def normalize(self, rows: Sequence[dict[str, Any]]) -> list[Document]: ...


def document_source_instances(
    items: Any, *, wiki_ids: set[str] | None = None
) -> list[SourceInstance]:
    """严格校验独立来源的配置；不接收任意包路径、驱动、接口或服务器路径。"""
    where = f"{SOURCE_FILE}.yaml 的 {SOURCE_INSTANCES}"
    if not isinstance(items, list):
        raise ValueError(f"{where} 要写成列表")
    known = platforms()
    seen = set(wiki_ids or ())
    found: list[SourceInstance] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{where} 的每项必须是来源配置")
        unknown = set(item) - _SOURCE_KEYS
        if unknown:
            raise ValueError(f"{where} 有不允许的项：{'、'.join(sorted(map(str, unknown)))}")
        source_id = item.get("id")
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise ValueError(f"{where} 的 id 要以小写字母开头，只用小写字母、数字、下划线")
        if source_id in seen:
            raise ValueError(f"来源 id「{source_id}」重复或与 Wiki id 重合")
        seen.add(source_id)
        platform_id = item.get("platform")
        if not isinstance(platform_id, str) or platform_id not in known:
            raise ValueError(f"来源「{source_id}」的 platform 必须是已登记的受信任平台")
        platform = known[platform_id]
        if platform.driver != DRIVER_DOCUMENTS:
            raise ValueError(f"来源「{source_id}」不是 documents 平台；Wiki 请使用 core.wiki.yaml")
        if EXTENSION_DOCUMENTS not in platform.capabilities:
            raise ValueError(f"平台「{platform.id}」没有 documents 能力")
        label = item.get("label", source_id)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"来源「{source_id}」的 label 要写成非空文本")
        mounted = item.get("mounted", True)
        if not isinstance(mounted, bool):
            raise ValueError(f"来源「{source_id}」的 mounted 必须是布尔值")
        role = item.get("role", "unknown")
        if not isinstance(role, str) or role not in SOURCE_ROLES:
            raise ValueError(f"来源「{source_id}」的 role 必须是 unknown、official 或 community")
        raw = {**item, "id": source_id, "label": label.strip(), "mounted": mounted, "role": role}
        if "collector" in item:
            if platform.id != "weibo":
                raise ValueError("collector 仅支持微博平台")
            raw["collector"] = parse_weibo_collector(item["collector"])
        found.append(
            SourceInstance(source_id, label.strip(), platform, platform.driver, mounted, role, raw)
        )
    return found


def source_instances(config: AppConfig) -> list[SourceInstance]:
    """Wiki 按旧顺序投影在前，独立文档来源在后；含停用来源。"""
    from jianshanagent.core.config import disabled_wiki_ids

    if SOURCE_FILE in config.override_errors:
        raise ValueError(f"来源登记覆写无效：{config.override_errors[SOURCE_FILE]}")
    raw = config.get(SOURCE_FILE)
    if SOURCE_FILE in config.files and SOURCE_INSTANCES not in raw:
        raise ValueError("core.sources.yaml 必须保留 sources 列表，不能删除来源登记")
    disabled = disabled_wiki_ids(config)
    found = [
        SourceInstance(
            entry.id,
            str(entry.raw.get("label") or "").strip() or entry.id,
            entry.platform,
            entry.platform.driver,
            entry.id not in disabled,
            "unknown",
            dict(entry.raw),
        )
        for entry in config.wikis
    ]
    items = raw.get(SOURCE_INSTANCES, [])
    return found + document_source_instances(items, wiki_ids={entry.id for entry in config.wikis})


def source_instance(config: AppConfig, source_id: str) -> SourceInstance:
    for source in source_instances(config):
        if source.id == source_id:
            return source
    raise ValueError(f"没有登记叫「{source_id}」的来源")


def disabled_source_ids(config: AppConfig) -> set[str]:
    return {source.id for source in source_instances(config) if not source.mounted}


def source_labels(config: AppConfig) -> dict[str, str]:
    return {source.id: source.label for source in source_instances(config)}


def retained_source_ids(config: AppConfig) -> set[str]:
    """全部登记来源，无论是否挂载。旧空 namespace 的归属仍由旧首 Wiki 规则处理。"""
    return {source.id for source in source_instances(config)}


def load_source_adapter(config: AppConfig, source_id: str) -> SourceAdapter:
    """只导入已登记平台的受信任 package，后台配置不能指定代码入口。"""
    source = source_instance(config, source_id)
    if source.driver != DRIVER_DOCUMENTS:
        raise PluginError(f"来源「{source_id}」是 Wiki，应使用现有 Wiki 扩展入口")
    if not source.mounted:
        raise PluginError(f"来源「{source_id}」已停用")
    if EXTENSION_DOCUMENTS not in source.platform.capabilities:
        raise PluginError(f"平台「{source.platform.id}」没有 documents 能力")
    module = importlib.import_module(source.platform.package)
    factory = getattr(module, "create_source_adapter", None)
    if not callable(factory):
        raise PluginError(f"平台「{source.platform.id}」没有 create_source_adapter(setup)")
    adapter = factory(SourceSetup(config.for_source(source_id), source))
    if not callable(getattr(adapter, "normalize", None)):
        raise PluginError(f"平台「{source.platform.id}」的来源适配器没有 normalize(rows)")
    return cast(SourceAdapter, adapter)
