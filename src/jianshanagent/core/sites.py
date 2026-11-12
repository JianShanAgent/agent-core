"""平台和站点插件的加载：功能插件开出扩展点，平台、站点插件往里接东西。

它们长什么样、清单怎么写见 ``core/manifest.py``。这里是运行时的那一半，所有扩展点共用一套：

- 开出扩展点的功能插件对挂着的每个 wiki 调一次 :func:`load_sites`，拿到这个 wiki 的平台和站点插件
  在这个扩展点上的实例，平台在前。
- 清单的 ``capabilities`` 里写了哪个扩展点，包里就要有同名的模块，模块里有
  ``create(setup)``，返回那个扩展点的基类的实例。基类在开出扩展点的功能插件里：
  ``plugins/wiki_query/provider.py`` 的 ``WikiProvider``、
  ``plugins/knowledge/site.py`` 的 ``SitePlugin``。
- 站点插件要用平台的东西（阴阳师要用灰机的 Data 接口）：``setup.platform(平台的类)``。
  平台没有接这个扩展点，在这里统一报错。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from jianshanagent.core.manifest import KIND_PLATFORM, Manifest, PluginError

if TYPE_CHECKING:
    from jianshanagent.core.config import AppConfig
    from jianshanagent.core.wiki import WikiSite

#: 接扩展点的模块里，入口函数叫这个名字
ENTRY = "create"
#: 现在有的两个扩展点（名字和开出它的功能插件的清单里写的一样）
EXTENSION_WIKI = "wiki"
EXTENSION_KNOWLEDGE = "knowledge"

#: 平台、站点插件装不上：找不到、不合规范
SiteError = PluginError

T = TypeVar("T")


@dataclass(slots=True)
class SiteSetup:
    """``create(setup)`` 拿到的东西。"""

    manifest: Manifest
    #: 这个 wiki 的那一份配置（通用的配置加上它的平台、站点插件的补充）
    config: AppConfig
    #: 自己的配置（目录里的 ``config.yaml``，加上后台改过的），没有就是空的
    raw: dict[str, Any]
    #: 是为哪个 wiki 加载的
    wiki: WikiSite
    #: 这个 wiki 的平台在同一个扩展点上的实例；平台没接这个扩展点（或者自己就是平台）是 None
    above: Any = None

    @property
    def id(self) -> str:
        return self.manifest.id

    def platform(self, kind: type[T]) -> T:
        """平台在这个扩展点上的实例（``kind`` 是它的类）。

        没有就报错：要么清单里的 ``platform`` 没写对，要么那个平台没接这个扩展点。
        """
        if isinstance(self.above, kind):
            return self.above
        raise SiteError(
            f"{self.manifest.what}要用平台的 {kind.__name__}，但没找到："
            f"它清单里写的平台是「{self.manifest.platform or '（没写）'}」，"
            "那个平台要在这个扩展点上提供它"
        )


def load_sites(config: AppConfig, extension: str, wiki: WikiSite) -> list[Any]:
    """``wiki`` 这个 wiki 的平台和站点插件在 ``extension`` 这个扩展点上的实例，平台在前。

    没接这个扩展点的跳过。拿到的配置是这个 wiki 的那一份。
    """
    loaded: list[Any] = []
    chain = config.wiki(wiki.id).chain
    config = config.for_wiki(wiki.id)
    above: Any = None
    for manifest in chain:
        if extension not in manifest.capabilities:
            continue
        path = f"{manifest.package}.{extension}"
        try:
            module = importlib.import_module(path)
        except ModuleNotFoundError as exc:
            if exc.name == path:
                raise SiteError(
                    f"{manifest.what}的清单里写了 {extension}，包里却没有这个模块（{path}）"
                ) from exc
            raise  # 模块找到了，是它里面要用的别的东西没装
        factory = getattr(module, ENTRY, None)
        if not callable(factory):
            raise SiteError(f"{manifest.what}的 {path} 里没有 {ENTRY}(setup)")
        setup = SiteSetup(
            manifest=manifest,
            config=config,
            raw=config.get(manifest.config_name),
            wiki=wiki,
            above=above,
        )
        instance = factory(setup)
        if getattr(instance, "id", None) != manifest.id:
            raise SiteError(
                f"{manifest.what}的 {path}.{ENTRY} 返回的对象 id 是"
                f"「{getattr(instance, 'id', None)}」，要和清单里的 id 一样"
            )
        loaded.append(instance)
        if manifest.kind == KIND_PLATFORM:
            above = instance
    return loaded
