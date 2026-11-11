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

**站点插件**（``kind: site``）——某个 wiki 平台、某个站特有的东西。通用的代码只懂 MediaWiki。
站点插件是一棵树：平台在上（``huiji`` = 灰机），具体的站在它下面（``yys`` = 阴阳师 wiki），
清单里用 ``parent`` 写明上级。核心不带任何站点插件，每个都是独立的包，登记到
``jianshanagent.sites`` 这个 entry points 组::

    jianshanagent_site_huiji/          平台（仓库 site-huiji）
      plugin.yaml  config.yaml  knowledge.py  wiki.py
    jianshanagent_site_huiji_yys/      这个平台上的一个站（仓库 site-huiji-yys）
      plugin.yaml（parent: huiji）  config.yaml  knowledge/  wiki.py

用哪个站写在 ``core.wiki.yaml`` 的 ``site`` 里，只写最末一级的 id（``site: yys``），上级顺着清单
自动带出来，从上到下依次加载；不写就是纯 MediaWiki。没登记的包也能用：``site`` 写它的包路径
（``mypkg.mysite``）。一个包也可以把下级放在自己的 ``sites/`` 目录里一起带上。
配置叫 ``site.<id>``。

站点插件的能力（清单里的 ``capabilities``）只有这几种，写了哪种就得有哪种：

- 功能插件开出来的扩展点（现在有 ``wiki`` = 查询，``knowledge`` = 知识库）：包里要有同名的模块
  （``wiki.py`` 或 ``wiki/``），模块里有 ``create(setup)``，返回那个扩展点的基类的实例
  （见 ``core/sites.py``；基类在开出扩展点的功能插件里）。
- ``extend``：对通用配置的补充。写在自己 ``config.yaml`` 的 ``extend`` 段里
  （配置名 → 要并进去的内容），读配置时就并进去了：名单合在一起，单个值以站点插件给的为准
  （见 ``core/config.py``）。
  这个站的清洗规则、提示词里的说法、人设都走这里。

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
    站点插件   仓库 site-<平台>[-<站>]      包 jianshanagent_site_<平台>[_<站>]   id 是最后一段
    部署插件   目录 deploy/plugins/<id>/

清单的格式（不认识的键会报错，免得写错了键名却以为生效了）::

    id: yys                    # 自带的和部署插件：和目录名一样；登记的包：和登记的名字一样
    kind: site                 # feature / site / deploy
    name: 阴阳师 wiki           # 给人看的名字
    description: …             # 一两句话说明它管什么
    # 只有站点插件有：
    parent: huiji              # 上级；最上面一级不写
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
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

MANIFEST_FILE = "plugin.yaml"
CONFIG_FILE = "config.yaml"
EXTRA_CONFIGS_DIR = "configs"
KIND_FEATURE = "feature"
KIND_SITE = "site"
KIND_DEPLOY = "deploy"
#: 部署插件的 Compose 片段、上线前的准备脚本
COMPOSE_FILE = "compose.yaml"
INIT_FILE = "init.sh"
FEATURES_PACKAGE = "jianshanagent.plugins"
#: 别的发行包登记插件用的 entry points 组：名字 = 插件 id，值 = 插件的包路径
ENTRY_POINTS = {KIND_FEATURE: "jianshanagent.plugins", KIND_SITE: "jianshanagent.sites"}
#: 站点插件的下级放在它目录里的这个子目录下
CHILDREN_DIR = "sites"
#: 站点插件的能力里不是扩展点的那一种：对通用配置的补充
CAPABILITY_EXTEND = "extend"

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_KEYS = {
    KIND_FEATURE: {"id", "kind", "name", "description", "after", "extension_points"},
    KIND_SITE: {"id", "kind", "name", "description", "parent", "capabilities"},
    KIND_DEPLOY: {"id", "kind", "name", "description", "services"},
}
_CONFIG_PREFIX = {KIND_FEATURE: "plugin.", KIND_SITE: "site."}


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
    #: 站点插件的上级（id，或者别人写的插件的包路径）；最上面一级是空的
    parent: str = ""
    #: 站点插件的能力
    capabilities: tuple[str, ...] = ()
    #: 功能插件：这些插件也启用时，要排在它们后面
    after: tuple[str, ...] = ()
    #: 功能插件：开给站点插件的扩展点
    extension_points: tuple[str, ...] = ()
    #: 部署插件：它的 Compose 片段里有哪些服务
    services: tuple[str, ...] = ()

    @property
    def config_name(self) -> str:
        """这个插件的配置叫什么：``plugin.chat`` / ``site.yys``。

        部署插件没有配置文件（它的参数是 Compose 变量）。
        """
        if self.kind not in _CONFIG_PREFIX:
            raise PluginError(f"{self.kind} 插件「{self.id}」没有配置文件，参数写在部署的 .env 里")
        return f"{_CONFIG_PREFIX[self.kind]}{self.id}"

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
    where = directory / MANIFEST_FILE
    try:
        raw = yaml.safe_load(where.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginError(f"{directory} 里没有插件清单（{MANIFEST_FILE}）") from exc
    except yaml.YAMLError as exc:
        raise PluginError(f"{where} 不是合法的 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise PluginError(f"{where} 要写成键值结构")
    if raw.get("kind") != kind:
        raise PluginError(f"{where} 的 kind 应该是 {kind}，现在是「{raw.get('kind')}」")
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
    if kind == KIND_SITE:
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
        parent=str(raw.get("parent") or ""),
        capabilities=capabilities,
        after=_names(raw, "after", where),
        extension_points=_names(raw, "extension_points", where),
        services=_services(raw, where) if kind == KIND_DEPLOY else (),
    )


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


def _plugin_dirs(directory: Path) -> list[Path]:
    """``directory`` 下面带清单的子目录，按名字排。"""
    if not directory.is_dir():
        return []
    return sorted(child for child in directory.iterdir() if (child / MANIFEST_FILE).is_file())


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


def _check_placement(manifest: Manifest, parent: str) -> None:
    """放在「parent」下面（空的 = 最上面一级）的插件，清单里写的上级得是它。"""
    if manifest.parent != parent:
        expected = f"parent: {parent}" if parent else "不写 parent"
        raise PluginError(
            f"{manifest.path / MANIFEST_FILE}：它放在「{parent or '最上面一级'}」下面，"
            f"清单里应该{expected}，现在是「{manifest.parent or '没写'}」"
        )


def _scan_sites(directory: Path, package: str, parent: str, found: dict[str, Manifest]) -> None:
    for child in _plugin_dirs(directory):
        manifest = read_manifest(child, f"{package}.{child.name}", KIND_SITE)
        _check_placement(manifest, parent)
        if manifest.id in found:
            raise PluginError(
                f"站点插件的 id「{manifest.id}」重了：{found[manifest.id].path} 和 {child}"
            )
        found[manifest.id] = manifest
        _scan_sites(child / CHILDREN_DIR, f"{manifest.package}.{CHILDREN_DIR}", manifest.id, found)


def _local_sites(workspace_root: Path | None) -> dict[str, Manifest]:
    """实例目录下的 ``sites/<id>/``：用户自写的站点插件（``parent`` 可指向上级包或同目录上级）。"""
    if workspace_root is None:
        return {}
    sites_dir = workspace_root / CHILDREN_DIR
    if not sites_dir.is_dir():
        return {}
    dirs = _plugin_dirs(sites_dir)
    if not dirs:
        return {}
    _ensure_sys_path(workspace_root)
    found: dict[str, Manifest] = {}
    for child in dirs:
        manifest = read_manifest(child, f"{CHILDREN_DIR}.{child.name}", KIND_SITE)
        found[manifest.id] = manifest
        _scan_sites(child / CHILDREN_DIR, f"{manifest.package}.{CHILDREN_DIR}", manifest.id, found)
    return found


@lru_cache(maxsize=1)
def _installed_package_sites() -> dict[str, Manifest]:
    """这个环境里登记了的站点插件，连同它们放在自己 ``sites/`` 里的下级：id → 清单。"""
    found = _registered(KIND_SITE)
    for manifest in list(found.values()):
        _scan_sites(
            manifest.path / CHILDREN_DIR, f"{manifest.package}.{CHILDREN_DIR}", manifest.id, found
        )
    return found


@lru_cache(maxsize=8)
def installed_sites(workspace_root: Path | None = None) -> dict[str, Manifest]:
    """环境里登记的站点插件，连同 ``sites/`` 下的下级及实例目录内的 ``sites/``：id → 清单。"""
    base = _installed_package_sites()
    if workspace_root is None:
        return base
    found = dict(base)
    for name, manifest in _local_sites(workspace_root.resolve()).items():
        if name in found:
            raise PluginError(f"站点插件的 id「{name}」重了：{found[name].path} 和 {manifest.path}")
        found[name] = manifest
    return found


def clear_caches() -> None:
    """忘掉已经找到的插件（装了、卸了插件以后，或者测试里换了登记的插件以后）。"""
    _installed_package_features.cache_clear()
    feature_manifests.cache_clear()
    extension_points.cache_clear()
    _installed_package_sites.cache_clear()
    installed_sites.cache_clear()


def _site(name: str, workspace_root: Path | None = None) -> Manifest:
    if "." in name:
        # 没登记的包：按包路径找
        if workspace_root is not None:
            _ensure_sys_path(workspace_root)
        return read_manifest(_package_dir(name, "站点插件"), name, KIND_SITE)
    installed = installed_sites(workspace_root)
    if name not in installed:
        raise PluginError(
            f"没有叫「{name}」的站点插件（装着的有：{'、'.join(installed) or '无'}；"
            "没登记的包要写包路径，如 mypkg.mysite，或放在实例的 sites/<id>/ 下）"
        )
    return installed[name]


def _parent_of(manifest: Manifest, workspace_root: Path | None = None) -> Manifest | None:
    # 放在上级插件目录的 sites/ 里的：上级就是外面那一层，清单里写的得是它
    enclosing = manifest.path.parent.parent
    if manifest.path.parent.name == CHILDREN_DIR and (enclosing / MANIFEST_FILE).is_file():
        _check_placement(manifest, enclosing.name)
        known = installed_sites(workspace_root).get(manifest.parent)
        if known is not None and known.path == enclosing:
            return known
        outer = manifest.package.rpartition(f".{CHILDREN_DIR}.")[0]
        return read_manifest(enclosing, outer, KIND_SITE)
    return _site(manifest.parent, workspace_root) if manifest.parent else None


def site_chain(name: str, workspace_root: Path | None = None) -> tuple[Manifest, ...]:
    """``core.wiki.yaml`` 的 ``site`` → 要加载的站点插件，从最上面一级到写的那一级。

    ``name`` 是最末一级：已登记或实例 ``sites/`` 下的写 id，未登记的包写包路径；空表示不用。
    """
    name = name.strip()
    if not name:
        return ()
    chain = [_site(name, workspace_root)]
    while (parent := _parent_of(chain[0], workspace_root)) is not None:
        if any(parent.id == manifest.id for manifest in chain):
            names = " → ".join(manifest.id for manifest in (parent, *chain))
            raise PluginError(f"站点插件的上级绕成了圈：{names}")
        chain.insert(0, parent)
    return tuple(chain)
