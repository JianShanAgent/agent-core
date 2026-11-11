"""站点插件的加载：功能插件开出扩展点，站点插件往里接东西。

插件长什么样、清单怎么写见 ``core/manifest.py``。这里是运行时的那一半，所有扩展点共用一套：

- 开出扩展点的功能插件调 :func:`load_sites`，拿到用到的各级站点插件在这个扩展点上的实例，
  从上到下。
- 站点插件在清单的 ``capabilities`` 里写了哪个扩展点，包里就要有同名的模块，模块里有
  ``create(setup)``，返回那个扩展点的基类的实例。基类在开出扩展点的功能插件里：
  ``plugins/wiki_query/provider.py`` 的 ``WikiProvider``、
  ``plugins/knowledge/site.py`` 的 ``SitePlugin``。
- 下级要用上级的东西（阴阳师要用灰机的 Data 接口）：``setup.parent(上级的类)``。
  上级没有接这个扩展点，在这里统一报错。
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from jianshanagent.core.manifest import Manifest, PluginError

if TYPE_CHECKING:
    from jianshanagent.core.config import AppConfig
    from jianshanagent.core.wiki import WikiSite

#: 站点插件接扩展点的模块里，入口函数叫这个名字
ENTRY = "create"
#: 现在有的两个扩展点（名字和开出它的功能插件的清单里写的一样）
EXTENSION_WIKI = "wiki"
EXTENSION_KNOWLEDGE = "knowledge"

#: 站点插件装不上：找不到、不合规范、缺上级
SiteError = PluginError

T = TypeVar("T")


@dataclass(slots=True)
class SiteSetup:
    """``create(setup)`` 拿到的东西。"""

    manifest: Manifest
    config: AppConfig
    #: 这个插件自己的配置（它目录里的 ``config.yaml``，加上后台改过的），没有就是空的
    raw: dict[str, Any]
    wiki: WikiSite
    #: 各级上级在同一个扩展点上的实例：id → 实例（没接这个扩展点的上级不在里面）
    parents: Mapping[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.manifest.id

    def parent(self, kind: type[T]) -> T:
        """上级里 ``kind`` 这个类的实例。
        没有就报错：要么清单里没写对上级，要么上级没接这个扩展点。"""
        for instance in reversed(list(self.parents.values())):
            if isinstance(instance, kind):
                return instance
        above = self.manifest.parent or "（没写）"
        raise SiteError(
            f"站点插件「{self.id}」要用它上级的 {kind.__name__}，但没找到："
            f"它清单里写的上级是「{above}」，上级要在 capabilities 里接同一个扩展点"
        )


def load_sites(config: AppConfig, extension: str, wiki: WikiSite) -> list[Any]:
    """用到的各级站点插件在 ``extension`` 这个扩展点上的实例，从上到下。没接的那几级跳过。"""
    loaded: dict[str, Any] = {}
    for manifest in config.site_chain:
        if extension not in manifest.capabilities:
            continue
        path = f"{manifest.package}.{extension}"
        try:
            module = importlib.import_module(path)
        except ModuleNotFoundError as exc:
            if exc.name == path:
                raise SiteError(
                    f"站点插件「{manifest.id}」的清单里写了 {extension}，"
                    f"包里却没有这个模块（{path}）"
                ) from exc
            raise  # 模块找到了，是它里面要用的别的东西没装
        factory = getattr(module, ENTRY, None)
        if not callable(factory):
            raise SiteError(f"站点插件「{manifest.id}」的 {path} 里没有 {ENTRY}(setup)")
        setup = SiteSetup(
            manifest=manifest,
            config=config,
            raw=config.site(manifest.id),
            wiki=wiki,
            parents=dict(loaded),
        )
        instance = factory(setup)
        if getattr(instance, "id", None) != manifest.id:
            raise SiteError(
                f"站点插件「{manifest.id}」的 {path}.{ENTRY} 返回的对象 id 是"
                f"「{getattr(instance, 'id', None)}」，要和清单里的 id 一样"
            )
        loaded[manifest.id] = instance
    return list(loaded.values())
