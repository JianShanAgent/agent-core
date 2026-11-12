"""插件规范：一个插件是一个自成一体的目录，清单、默认配置、代码都在里面。

完整的说明（命名、目录、清单、怎么登记、怎么写文档）在 ``docs/插件规范.md``，
这里是给读代码的人看的要点。

::

    <插件目录>/
      plugin.yaml          清单：它是谁、依赖谁、有什么能力（格式见下）
      config.yaml          默认配置（没有可配的东西就不放）
      configs/             另外带的配置，名字照 config/ 里的取法（对话插件的人设在这里）；一般没有
      __init__.py …        代码（部署插件没有代码，是 Compose 片段和它的配置文件）

插件有三种，按「谁的事」分层：

**功能插件**（``kind: feature``）——机器人会做的事：查询、知识库、查证、对话、记忆……
核心自带的放在 ``jianshanagent/plugins/<id>/``；别的仓库写的是一个独立的包
（``jianshanagent_plugin_<id>``），在自己的 ``pyproject.toml`` 里登记到
``jianshanagent.plugins`` 这个 entry points 组（名字 = 插件 id，值 = 包路径）。
启用哪些写在 ``core.plugins.yaml`` 的 ``enabled`` 里。
包里要有 ``create_plugin()``，返回的对象有 ``id`` 和 ``register(ctx)``（见 ``core/plugin.py``）。
配置叫 ``plugin.<id>``。功能插件可以开出「扩展点」（清单里的 ``extension_points``），
让站点插件往里接东西。

**站点插件**（``kind: site``）——某一个站特有的东西。通用的代码只懂 MediaWiki；各个 wiki 平台
在 MediaWiki 之上多出来的东西是核心自带的（见下面的「平台」），一个具体的站的东西才由站点插件
带进来：``huiji_yys`` = 灰机上的阴阳师 wiki。清单里用 ``platform`` 写明它是哪个平台上的站，
id 以平台的 id 开头（``<平台>_<站>``）：同一个题材在两个平台上各有一个站时
（``huiji_yys``、``bwiki_yys``），它们能装在一起、挂在同一个部署上。自己架的 MediaWiki 站
不在哪个平台上，不写 ``platform``（就是 ``mediawiki``），id 直接用站名。

核心不带任何站点插件，每个都是独立的包（``jianshanagent_site_<id>``）。它们有三个来处：

- 装在 Python 环境里、登记到 ``jianshanagent.sites`` 这个 entry points 组的；
- 在管理后台里从站点目录下载的（见 ``core/catalog.py``），放在数据目录的 ``sites/`` 下。
  环境里已经装着同一个 id 的，用环境里的；
- 实例目录下的 ``sites/<id>/``：自己写的，免打包直接加载。

::

    jianshanagent_site_huiji_yys/      灰机上的一个站（仓库 site-huiji-yys）
      plugin.yaml（platform: huiji）  config.yaml  knowledge/  wiki.py

一个部署挂哪几个 wiki 写在 ``core.wiki.yaml`` 的 ``wikis`` 里，每个 wiki 用哪个站点插件写在它的
``site`` 里（``site: huiji_yys``），是哪个平台跟着站点插件走。没有站点插件的 wiki 写
``platform``（``platform: huiji``），只用平台的那一份；都不写就是纯 MediaWiki。
没登记的包也能用：``site`` 写它的包路径（``mypkg.mysite``）。配置叫 ``site.<id>``。

站点插件的能力（清单里的 ``capabilities``）只有这几种，写了哪种就得有哪种：

- 功能插件开出来的扩展点（现在有 ``wiki`` = 查询，``knowledge`` = 知识库）：包里要有同名的模块
  （``wiki.py`` 或 ``wiki/``），模块里有 ``create(setup)``，返回那个扩展点的基类的实例
  （见 ``core/sites.py``；基类在开出扩展点的功能插件里）。
- ``extend``：对通用配置的补充。写在自己 ``config.yaml`` 的 ``extend`` 段里
  （配置名 → 要并进去的内容），读配置时就并进去了：名单合在一起，单个值以站点插件给的为准
  （见 ``core/config.py``）。
  这个站的清洗规则、提示词里的说法、人设都走这里。

**平台**——一个 wiki 平台在 MediaWiki 之上多出来的东西：灰机的 Data 命名空间，
BWiki 的语义查询和取数的速率。平台不是插件，是核心自带的，在 ``jianshanagent/platforms/<id>/``::

    jianshanagent/platforms/huiji/
      platform.yaml  config.yaml  knowledge.py  wiki.py

``platform.yaml`` 里有 ``id``、``name``、``description``、``capabilities``，能力和站点插件的是
同一套、接法也一样；配置叫 ``platform.<id>``。挂着的 wiki 先加载它的平台，再加载它的站点插件，
站点插件要用平台的东西（阴阳师要用灰机的 Data 接口）：``setup.platform(平台的类)``。
现在有 ``huiji``、``bwiki``，和什么都不多带的 ``mediawiki``。兼容一个新平台是改核心的事。

**部署插件**（``kind: deploy``）——和机器人一起部署、但机器人离了它照样跑的服务。现在有 ``logs``
（运行日志：Loki + Alloy + Grafana）。它不在 Python 包里，在仓库的 ``deploy/plugins/<id>/``::

    deploy/plugins/logs/
      plugin.yaml          清单
      compose.yaml         Compose 片段：这个插件带来的服务。和 deploy/docker-compose.yml
                           合在一起用，相对路径从 deploy/ 算起，网络用主文件里的 jianshanagent
      init.sh              上线前的准备（可以没有）：建数据目录、补 .env 里缺的变量；
                           只添不改，能重复执行
      …                    服务自己的配置文件

带哪些写在服务器 ``shared/.env`` 的 ``JIANSHANAGENT_DEPLOY_PLUGINS`` 里（它是这台服务器的事，
不随发布走；一个都不带就留空）。``deploy/scripts/ctl`` 把列着的片段合进去，
``server-release.sh`` 在切换前跑它们的 ``init.sh``；从名单里去掉的，下次上线它的容器就没了，
数据留在盘上。
部署插件和机器人之间没有代码上的往来：机器人只管往标准输出打 JSON 日志，谁来收、收到哪去，
是部署插件的事——想换一套日志系统，就另写一个部署插件，在名单里换掉 ``logs``。
它的参数是 Compose 变量（``.env``），没有 ``config.yaml``。

命名（各层的仓库、发行包、Python 包、id 怎么取，见 ``docs/插件规范.md`` 的表）::

    功能插件   仓库 plugin-<id>            包 jianshanagent_plugin_<id>
    站点插件   仓库 site-<平台>-<站>        包 jianshanagent_site_<id>           id 是 <平台>_<站>
    部署插件   目录 deploy/plugins/<id>/

清单的格式（不认识的键会报错，免得写错了键名却以为生效了）::

    id: huiji_yys              # 自带的和部署插件：和目录名一样；登记的包：和登记的名字一样
    kind: site                 # feature / site / deploy
    name: 阴阳师 wiki           # 给人看的名字
    description: …             # 一两句话说明它管什么
    # 只有站点插件有：
    platform: huiji            # 它是哪个平台上的站；自己架的 MediaWiki 站不写
    capabilities: [wiki, knowledge, extend]
    # 只有功能插件有：
    after: [knowledge]         # 这些插件也启用时，要排在它们后面
    extension_points: [wiki]   # 开给站点插件的扩展点
    # 只有部署插件有：
    services: [loki, alloy]    # 它的 compose.yaml 里有哪些服务（要和文件里的一致，不能和别的重名）

这个模块只读清单、认目录，不导入插件的代码。
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

MANIFEST_FILE = "plugin.yaml"
#: 平台的清单（平台是核心自带的，不是插件）
PLATFORM_FILE = "platform.yaml"
CONFIG_FILE = "config.yaml"
EXTRA_CONFIGS_DIR = "configs"
KIND_FEATURE = "feature"
KIND_SITE = "site"
KIND_DEPLOY = "deploy"
KIND_PLATFORM = "platform"
#: 部署插件的 Compose 片段、上线前的准备脚本
COMPOSE_FILE = "compose.yaml"
INIT_FILE = "init.sh"
FEATURES_PACKAGE = "jianshanagent.plugins"
PLATFORMS_PACKAGE = "jianshanagent.platforms"
#: 不在哪个平台上的 MediaWiki 站算在这个平台上：它什么都不多带
DEFAULT_PLATFORM = "mediawiki"
#: 别的发行包登记插件用的 entry points 组：名字 = 插件 id，值 = 插件的包路径
ENTRY_POINTS = {KIND_FEATURE: "jianshanagent.plugins", KIND_SITE: "jianshanagent.sites"}
#: 实例目录下放自己写的站点插件的子目录
LOCAL_SITES_DIR = "sites"
#: 在管理后台里下载的站点插件放在哪（不设就是实例目录的 data/sites）
SITES_DIR_ENV = "JIANSHANAGENT_SITES_DIR"
#: 那个目录里记着「装了哪些、各是哪个版本」的文件，和它的格式号
SITES_STATE_FILE = "state.json"
SITES_STATE_FORMAT = 1
#: 站点插件的能力里不是扩展点的那一种：对通用配置的补充
CAPABILITY_EXTEND = "extend"

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_KEYS = {
    KIND_FEATURE: {"id", "kind", "name", "description", "after", "extension_points"},
    KIND_SITE: {"id", "kind", "name", "description", "platform", "capabilities"},
    KIND_DEPLOY: {"id", "kind", "name", "description", "services"},
    KIND_PLATFORM: {"id", "name", "description", "capabilities"},
}
_CONFIG_PREFIX = {KIND_FEATURE: "plugin.", KIND_SITE: "site.", KIND_PLATFORM: "platform."}
_MANIFEST_FILES = {KIND_PLATFORM: PLATFORM_FILE}
_WHAT = {
    KIND_FEATURE: "功能插件",
    KIND_SITE: "站点插件",
    KIND_DEPLOY: "部署插件",
    KIND_PLATFORM: "平台",
}


class PluginError(ValueError):
    """插件不合规范、找不到、装不上。"""


@dataclass(frozen=True, slots=True)
class Manifest:
    """一个插件的清单，加上它在哪。"""

    id: str
    kind: str
    name: str
    description: str
    #: 插件的目录
    path: Path
    #: 插件的包路径（``jianshanagent_site_huiji_yys``）；部署插件没有
    package: str
    #: 站点插件是哪个平台上的站（平台的 id）
    platform: str = ""
    #: 站点插件、平台的能力
    capabilities: tuple[str, ...] = ()
    #: 功能插件：这些插件也启用时，要排在它们后面
    after: tuple[str, ...] = ()
    #: 功能插件：开给站点插件的扩展点
    extension_points: tuple[str, ...] = ()
    #: 部署插件：它的 Compose 片段里有哪些服务
    services: tuple[str, ...] = ()

    @property
    def config_name(self) -> str:
        """这个插件的配置叫什么：``plugin.chat`` / ``site.huiji_yys``；平台的叫 ``platform.huiji``。

        部署插件没有配置文件（它的参数是 Compose 变量）。
        """
        if self.kind not in _CONFIG_PREFIX:
            raise PluginError(f"{self.kind} 插件「{self.id}」没有配置文件，参数写在部署的 .env 里")
        return f"{_CONFIG_PREFIX[self.kind]}{self.id}"

    @property
    def what(self) -> str:
        """报错、提示里怎么称呼它：「站点插件「huiji_yys」」/「平台「huiji」」。"""
        return f"{_WHAT[self.kind]}「{self.id}」"

    @property
    def compose_path(self) -> Path:
        """部署插件的 Compose 片段。"""
        return self.path / COMPOSE_FILE

    @property
    def config_path(self) -> Path:
        """默认配置在哪（文件不一定有）。"""
        return self.path / CONFIG_FILE

    @property
    def extra_configs(self) -> Path:
        """另外带的配置放在哪个目录下（目录不一定有）。"""
        return self.path / EXTRA_CONFIGS_DIR


def _names(raw: dict[str, Any], key: str, where: Path) -> tuple[str, ...]:
    value = raw.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PluginError(f"{where} 的 {key} 要写成名字的列表")
    return tuple(value)


def read_manifest(directory: Path, package: str, kind: str, plugin_id: str = "") -> Manifest:
    """读一个插件目录里的清单，不合规范就报错。

    ``plugin_id``：这个插件是按什么名字登记的（entry points 里的名字）；不给就是目录名。
    """
    file = _MANIFEST_FILES.get(kind, MANIFEST_FILE)
    where = directory / file
    try:
        raw = yaml.safe_load(where.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginError(f"{directory} 里没有清单（{file}）") from exc
    except yaml.YAMLError as exc:
        raise PluginError(f"{where} 不是合法的 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise PluginError(f"{where} 要写成键值结构")
    if kind != KIND_PLATFORM and raw.get("kind") != kind:
        raise PluginError(f"{where} 的 kind 应该是 {kind}，现在是「{raw.get('kind')}」")
    if kind == KIND_SITE and "parent" in raw:
        raise PluginError(
            f"{where} 的 parent 不用了：平台是核心自带的，站点插件只有一层。"
            f"改成 platform: {raw.get('parent')}（它是哪个平台上的站）"
        )
    unknown = sorted(set(raw) - _KEYS[kind])
    if unknown:
        raise PluginError(f"{where} 里有不认识的项：{'、'.join(unknown)}")
    expected, what = (plugin_id, "登记的名字") if plugin_id else (directory.name, "目录名")
    plugin_id = str(raw.get("id") or "")
    if plugin_id != expected:
        raise PluginError(f"{where} 的 id（{plugin_id or '没写'}）要和{what}（{expected}）一样")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise PluginError(f"{where} 没有写 name（给人看的名字）")
    capabilities = _names(raw, "capabilities", where)
    if kind in (KIND_SITE, KIND_PLATFORM):
        allowed = {*extension_points(), CAPABILITY_EXTEND}
        wrong = [item for item in capabilities if item not in allowed]
        if wrong:
            raise PluginError(
                f"{where} 的 capabilities 里有不认识的能力：{'、'.join(wrong)}"
                f"（现在有 {'、'.join(sorted(allowed))}）"
            )
    return Manifest(
        id=plugin_id,
        kind=kind,
        name=name,
        description=" ".join(str(raw.get("description") or "").split()),
        path=directory,
        package=package,
        platform=_platform_of(plugin_id, raw, where) if kind == KIND_SITE else "",
        capabilities=capabilities,
        after=_names(raw, "after", where),
        extension_points=_names(raw, "extension_points", where),
        services=_services(raw, where) if kind == KIND_DEPLOY else (),
    )


def _platform_of(site_id: str, raw: dict[str, Any], where: Path) -> str:
    """站点插件清单里的 ``platform``：得是核心带着的平台，id 要照平台取。"""
    known = platforms()
    name = str(raw.get("platform") or DEFAULT_PLATFORM)
    if name not in known:
        raise PluginError(
            f"{where} 的 platform 写的是「{name}」，核心里没有这个平台"
            f"（现在有 {'、'.join(known)}；自己架的 MediaWiki 站不写 platform）"
        )
    if name != DEFAULT_PLATFORM:
        if not site_id.startswith(f"{name}_") or site_id == f"{name}_":
            raise PluginError(
                f"{where}：它是「{name}」平台上的站，id 要以平台的 id 开头："
                f"{name}_<这个站的名字>（如 {name}_{site_id.rpartition('_')[2] or 'mysite'}）"
            )
        return name
    taken = next((other for other in known if site_id.startswith(f"{other}_")), "")
    if taken and taken != DEFAULT_PLATFORM:
        raise PluginError(
            f"{where} 的 id（{site_id}）以平台「{taken}」的 id 开头，"
            f"清单里却没有写 platform: {taken}"
        )
    return name


def _services(raw: dict[str, Any], where: Path) -> tuple[str, ...]:
    """部署插件清单里的 ``services``：要有，而且和它的 Compose 片段里写的是同一批。"""
    listed = _names(raw, "services", where)
    if not listed:
        raise PluginError(f"{where} 没有写 services（这个插件带来哪些服务）")
    compose = where.parent / COMPOSE_FILE
    try:
        content = yaml.safe_load(compose.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginError(f"{where.parent} 里没有 Compose 片段（{COMPOSE_FILE}）") from exc
    except yaml.YAMLError as exc:
        raise PluginError(f"{compose} 不是合法的 YAML：{exc}") from exc
    actual = sorted((content or {}).get("services") or {}) if isinstance(content, dict) else []
    if actual != sorted(listed):
        raise PluginError(
            f"{where} 的 services（{'、'.join(listed)}）和 {COMPOSE_FILE} 里的服务"
            f"（{'、'.join(actual) or '没有'}）对不上"
        )
    return listed


def deploy_manifests(directory: Path) -> dict[str, Manifest]:
    """``directory``（仓库的 ``deploy/plugins``）下面的部署插件：id → 清单。

    部署插件不在 Python 包里，机器人运行时也看不到它们；这个函数是给检查用的（测试、以后的工具）。
    两个插件带了同名的服务会报错——合在一起时后一个会悄悄盖掉前一个。
    """
    found: dict[str, Manifest] = {}
    owner: dict[str, str] = {}
    for child in _plugin_dirs(directory):
        manifest = read_manifest(child, "", KIND_DEPLOY)
        for service in manifest.services:
            if service in owner:
                raise PluginError(
                    f"部署插件「{manifest.id}」和「{owner[service]}」都带了叫 {service} 的服务"
                )
            owner[service] = manifest.id
        found[manifest.id] = manifest
    return found


def _plugin_dirs(directory: Path, file: str = MANIFEST_FILE) -> list[Path]:
    """``directory`` 下面带清单的子目录，按名字排。"""
    if not directory.is_dir():
        return []
    return sorted(child for child in directory.iterdir() if (child / file).is_file())


def _package_dir(package: str, what: str) -> Path:
    """装在这个 Python 环境里的包 ``package`` 的目录。只导入它上面的包，不导入它自己。"""
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError) as exc:
        raise PluginError(f"找不到{what}「{package}」：{exc}") from exc
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    if not locations:
        raise PluginError(f"找不到{what}「{package}」（要是一个装在这个 Python 环境里的包）")
    return Path(locations[0])


def _entry_points(group: str) -> list[tuple[str, str]]:
    """这个环境里登记在 ``group`` 下的插件：（登记的名字，包路径）。测试会换掉这个函数。"""
    return sorted(
        (entry.name, entry.value.split(":")[0].strip())
        for entry in importlib.metadata.entry_points(group=group)
    )


def _registered(kind: str) -> dict[str, Manifest]:
    """别的发行包登记的插件：id → 清单。"""
    what = "功能插件" if kind == KIND_FEATURE else "站点插件"
    found: dict[str, Manifest] = {}
    for name, package in _entry_points(ENTRY_POINTS[kind]):
        if name in found:
            raise PluginError(
                f"{what}的 id「{name}」重了：{found[name].package} 和 {package} 都登记了它"
            )
        found[name] = read_manifest(_package_dir(package, what), package, kind, plugin_id=name)
    return found


def _ensure_sys_path(root: Path) -> None:
    resolved = str(root.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def _local_features(workspace_root: Path | None) -> dict[str, Manifest]:
    """实例目录下的 ``plugins/<id>/``：用户自己写的功能插件，免打包直接加载。"""
    if workspace_root is None:
        return {}
    plugins_dir = workspace_root / "plugins"
    if not plugins_dir.is_dir():
        return {}
    found: dict[str, Manifest] = {}
    dirs = _plugin_dirs(plugins_dir)
    if dirs:
        _ensure_sys_path(workspace_root)
    for directory in dirs:
        package = f"plugins.{directory.name}"
        found[directory.name] = read_manifest(directory, package, KIND_FEATURE)
    return found


@lru_cache(maxsize=1)
def _installed_package_features() -> dict[str, Manifest]:
    """核心自带的与装成包登记的功能插件：id → 清单。"""
    found: dict[str, Manifest] = {}
    for directory in _plugin_dirs(_PACKAGE_ROOT / "plugins"):
        package = f"{FEATURES_PACKAGE}.{directory.name}"
        found[directory.name] = read_manifest(directory, package, KIND_FEATURE)
    for name, manifest in _registered(KIND_FEATURE).items():
        if name in found:
            raise PluginError(f"功能插件的 id「{name}」重了：核心自带的和 {manifest.package}")
        found[name] = manifest
    return found


@lru_cache(maxsize=8)
def feature_manifests(workspace_root: Path | None = None) -> dict[str, Manifest]:
    """能用的功能插件（核心自带的、装成包登记的，以及实例目录下的 ``plugins/``）：id → 清单。"""
    base = _installed_package_features()
    if workspace_root is None:
        return base
    found = dict(base)
    for name, manifest in _local_features(workspace_root.resolve()).items():
        if name in found:
            raise PluginError(
                f"功能插件的 id「{name}」重了：{found[name].package} 和 {manifest.path}"
            )
        found[name] = manifest
    return found


@lru_cache(maxsize=1)
def extension_points() -> frozenset[str]:
    """功能插件开出来的全部扩展点。"""
    return frozenset(
        point for manifest in feature_manifests().values() for point in manifest.extension_points
    )


@lru_cache(maxsize=1)
def platforms() -> dict[str, Manifest]:
    """核心带着的平台：id → 清单。"""
    found: dict[str, Manifest] = {}
    for directory in _plugin_dirs(_PACKAGE_ROOT / "platforms", PLATFORM_FILE):
        package = f"{PLATFORMS_PACKAGE}.{directory.name}"
        found[directory.name] = read_manifest(directory, package, KIND_PLATFORM)
    if DEFAULT_PLATFORM not in found:
        where = _PACKAGE_ROOT / "platforms"
        raise PluginError(f"核心里少了「{DEFAULT_PLATFORM}」这个平台（{where}）")
    return found


def platform(name: str) -> Manifest:
    """叫 ``name`` 的平台；空的就是什么都不多带的 MediaWiki。没有就报错。"""
    known = platforms()
    name = name.strip() or DEFAULT_PLATFORM
    if name not in known:
        raise PluginError(f"核心里没有叫「{name}」的平台（现在有 {'、'.join(known)}）")
    return known[name]


def _local_sites(workspace_root: Path | None) -> dict[str, Manifest]:
    """实例目录下的 ``sites/<id>/``：用户自己写的站点插件，免打包直接加载。"""
    if workspace_root is None:
        return {}
    dirs = _plugin_dirs(workspace_root / LOCAL_SITES_DIR)
    if not dirs:
        return {}
    _ensure_sys_path(workspace_root)
    return {
        child.name: read_manifest(child, f"{LOCAL_SITES_DIR}.{child.name}", KIND_SITE)
        for child in dirs
    }


def downloaded_sites_dir(workspace_root: Path | None) -> Path | None:
    """在管理后台里下载的站点插件放在哪；找不到实例目录、也没设环境变量时是 None。"""
    explicit = os.environ.get(SITES_DIR_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return workspace_root / "data" / "sites" if workspace_root is not None else None


def read_sites_state(directory: Path) -> dict[str, dict[str, Any]]:
    """下载目录里记着的「装了哪些」：id → ``{"version": …, "repo": …, "installed_at": …}``。

    没有、读不了、格式不认识，都是空的。
    """
    try:
        data = json.loads((directory / SITES_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != SITES_STATE_FORMAT:
        return {}
    sites = data.get("sites")
    if not isinstance(sites, dict):
        return {}
    return {str(key): value for key, value in sites.items() if isinstance(value, dict)}


def downloaded_site_dir(directory: Path, site_id: str, version: str) -> Path:
    """下载的站点插件解在哪：这个目录里是它的包（``jianshanagent_site_<id>/``）。"""
    return directory / site_id / version


def site_package(site_id: str) -> str:
    """照命名规范，站点插件的包叫什么。"""
    return f"jianshanagent_site_{site_id}"


def _downloaded_sites(workspace_root: Path | None, skip: set[str]) -> dict[str, Manifest]:
    """在管理后台里下载的站点插件：id → 清单。``skip`` 里的（别处已经有的）不看。

    哪一个坏了（目录没了、清单读不出来）就跳过它：下载的东西坏了不该拦住机器人启动，
    用到它的 wiki 那里会说找不到这个插件。
    """
    directory = downloaded_sites_dir(workspace_root)
    if directory is None:
        return {}
    found: dict[str, Manifest] = {}
    for site_id, entry in sorted(read_sites_state(directory).items()):
        if site_id in skip:
            continue
        root = downloaded_site_dir(directory, site_id, str(entry.get("version") or ""))
        package = site_package(site_id)
        try:
            manifest = read_manifest(root / package, package, KIND_SITE, plugin_id=site_id)
        except PluginError:
            continue
        _ensure_sys_path(root)
        found[site_id] = manifest
    return found


@lru_cache(maxsize=1)
def _installed_package_sites() -> dict[str, Manifest]:
    """这个环境里登记了的站点插件：id → 清单。"""
    return _registered(KIND_SITE)


@lru_cache(maxsize=8)
def installed_sites(workspace_root: Path | None = None) -> dict[str, Manifest]:
    """能用的站点插件（环境里登记的、实例目录下 ``sites/`` 里的、在管理后台里下载的）：id → 清单。

    下载的和别处的重了，用别处的：环境里装的那一份是发布定下来的，自己写的是正在改的。
    """
    found = dict(_installed_package_sites())
    root = workspace_root.resolve() if workspace_root is not None else None
    for name, manifest in _local_sites(root).items():
        if name in found:
            raise PluginError(f"站点插件的 id「{name}」重了：{found[name].path} 和 {manifest.path}")
        found[name] = manifest
    found |= _downloaded_sites(root, set(found))
    return found


def site_source(manifest: Manifest, workspace_root: Path | None = None) -> str:
    """一个站点插件是哪来的。

    ``environment``（环境里装的）/ ``local``（实例的 ``sites/`` 下的）/ ``downloaded``
    （在管理后台里下载的）/ ``path``（按包路径写的）。
    """
    if _installed_package_sites().get(manifest.id) == manifest:
        return "environment"
    directory = downloaded_sites_dir(workspace_root)
    if directory is not None and directory.resolve() in manifest.path.resolve().parents:
        return "downloaded"
    if workspace_root is not None and manifest.path.parent == workspace_root / LOCAL_SITES_DIR:
        return "local"
    return "path"


def clear_caches() -> None:
    """忘掉已经找到的插件（装了、卸了插件以后，或者测试里换了登记的插件以后）。"""
    importlib.invalidate_caches()  # 刚下载下来的包：导入系统记着的目录内容是旧的
    _installed_package_features.cache_clear()
    feature_manifests.cache_clear()
    extension_points.cache_clear()
    platforms.cache_clear()
    _installed_package_sites.cache_clear()
    installed_sites.cache_clear()


def site(name: str, workspace_root: Path | None = None) -> Manifest:
    """叫 ``name`` 的站点插件：装着的写 id，没登记的包写包路径。没有就报错。"""
    if "." in name:
        # 没登记的包：按包路径找
        if workspace_root is not None:
            _ensure_sys_path(workspace_root)
        return read_manifest(_package_dir(name, "站点插件"), name, KIND_SITE)
    installed = installed_sites(workspace_root)
    if name not in installed:
        raise PluginError(
            f"没有叫「{name}」的站点插件（装着的有：{'、'.join(installed) or '无'}；"
            "做好了的站点插件可以在管理后台的「Wiki」页里一键添加，"
            "没登记的包要写包路径，如 mypkg.mysite，或放在实例的 sites/<id>/ 下）"
        )
    return installed[name]


def wiki_plugins(
    platform_name: str, site_name: str, workspace_root: Path | None = None
) -> tuple[Manifest, Manifest | None]:
    """一个 wiki 在 ``core.wiki.yaml`` 里写的 ``platform`` 和 ``site`` →（它的平台，它的站点插件）。

    写了 ``site`` 的，平台跟着站点插件走（两样都写了就得对得上）；没写 ``site`` 的没有站点插件，
    平台是 ``platform`` 写的那个，也没写就是什么都不多带的 MediaWiki。
    """
    platform_name, site_name = platform_name.strip(), site_name.strip()
    if not site_name:
        return platform(platform_name), None
    manifest = site(site_name, workspace_root)
    if platform_name and platform_name != manifest.platform:
        raise PluginError(
            f"站点插件「{manifest.id}」是「{manifest.platform}」平台上的站，"
            f"platform 却写的是「{platform_name}」（写了 site 就不用再写 platform）"
        )
    return platform(manifest.platform), manifest
