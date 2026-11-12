"""插件规范（``core/manifest.py``、``core/sites.py``，说明在 ``docs/插件规范.md``）。

- 核心自带的功能插件都合规范：有清单，清单说的和代码里有的对得上，配置在插件自己的目录里；
- 核心带着平台（灰机、BWiki，和什么都不多带的 MediaWiki），不带站点插件。站点插件只有一层：
  别的包登记了（entry points）就能按 id 用，清单里写明它是哪个平台上的站，平台跟着它加载；
- 一个部署可以挂好几个 wiki，各有各的平台和站点插件，它们的补充只对那个 wiki 生效；
- 默认配置随包走，部署目录里只放和默认值不一样的地方；
- 不合规范的清单、接不上的扩展点、不照规矩取的名字，报错要说得清是哪里不对。
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from jianshanagent.core.config import DEFAULTS_DIR, AppConfig, load_config
from jianshanagent.core.manifest import (
    DEFAULT_PLATFORM,
    KIND_FEATURE,
    KIND_PLATFORM,
    PluginError,
    extension_points,
    feature_manifests,
    installed_sites,
    platforms,
    read_manifest,
    site,
    wiki_plugins,
)
from jianshanagent.core.plugin import PluginHost
from jianshanagent.core.sites import EXTENSION_KNOWLEDGE, EXTENSION_WIKI, SiteSetup, load_sites
from jianshanagent.core.wiki import WikiSite, load_wikis
from jianshanagent.platforms.huiji.knowledge import HuijiKnowledge
from jianshanagent.platforms.huiji.wiki import HuijiData
from jianshanagent.plugins.wiki_query.provider import WikiProvider
from jianshanagent.testing.conformance import check_plugin
from jianshanagent.testing.sites import one_wiki, register_plugins, site_packages, write_site

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "jianshanagent"
SITE = "https://wiki.example"
WIKI = WikiSite(api_url=f"{SITE}/api.php", site_base=SITE, id="main")


def config_for(site: str, **files: dict[str, Any]) -> AppConfig:
    return AppConfig(root=Path("."), files={"core.wiki": one_wiki(site), **files}, secrets={})


def test_the_core_ships_the_platforms() -> None:
    found = platforms()
    assert set(found) == {"huiji", "bwiki", DEFAULT_PLATFORM}
    packages = {p.parent.name for p in (SOURCE / "platforms").glob("*/platform.yaml")}
    assert packages == set(found)  # platforms/ 下面的每个目录都有清单
    for manifest in found.values():
        assert manifest.kind == KIND_PLATFORM and manifest.name and manifest.description
        assert manifest.path == SOURCE / "platforms" / manifest.id
        assert manifest.config_name == f"platform.{manifest.id}"
        assert manifest.what == f"平台「{manifest.id}」"
        # 清单里说的能力，目录里都有：接扩展点的模块有 create(setup)，补充写在配置的 extend 段里
        for capability in manifest.capabilities:
            if capability == "extend":
                extend = yaml.safe_load(manifest.config_path.read_text("utf-8"))["extend"]
                assert extend, manifest.id
            else:
                module = importlib.import_module(f"{manifest.package}.{capability}")
                assert callable(module.create), (manifest.id, capability)
    # 什么都不多带的那个：自己架的 MediaWiki 站算在它上面
    assert found[DEFAULT_PLATFORM].capabilities == ()
    assert not found[DEFAULT_PLATFORM].config_path.exists()
    assert found["huiji"].capabilities == ("wiki", "knowledge", "extend")
    assert found["bwiki"].capabilities == ("wiki", "extend")


def write_wikis(directory: Path, *wikis: dict[str, Any], **top: Any) -> None:
    """部署的 ``core.wiki.yaml``：挂这几个 wiki。"""
    directory.mkdir(exist_ok=True)
    (directory / "core.wiki.yaml").write_text(
        yaml.safe_dump({**top, "wikis": list(wikis)}, allow_unicode=True), "utf-8"
    )


@pytest.fixture(autouse=True)
def only_the_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """这个环境里真装着的插件当作没装：这些测试说的是核心自己。"""
    register_plugins(monkeypatch)


# ---------- 核心自带的 ----------


def test_every_shipped_feature_plugin_is_a_directory_with_a_manifest_config_and_code() -> None:
    features = feature_manifests()
    assert set(features) == {
        "chat", "chatlog", "knowledge", "memory", "research", "safety", "wiki_query",
    }  # fmt: skip
    # 核心不带站点插件：它们各自是一个包，装上、登记了才有
    assert installed_sites() == {} and not (SOURCE / "sites").exists()
    # plugins/ 下面的每个包都有清单（没有清单的插件装不上，不能悄悄漏掉）
    packages = {p.parent.name for p in (SOURCE / "plugins").glob("*/__init__.py")}
    assert packages == set(features)

    enabled = load_config(REPO / "config").get("core.plugins")["enabled"]
    assert set(enabled) <= set(features)
    for manifest in features.values():
        assert manifest.kind == KIND_FEATURE and manifest.name and manifest.description
        assert manifest.path == SOURCE / "plugins" / manifest.id
        assert manifest.config_name == f"plugin.{manifest.id}" and manifest.config_path.is_file()
        # 入口只有一种：包里的 create_plugin()，返回的对象 id 和清单一样
        module = importlib.import_module(manifest.package)
        # （包里叫 plugin 的只有那个子模块，不再另外导出一个现成的插件对象）
        assert isinstance(getattr(module, "plugin", None), types.ModuleType | None), manifest.id
        assert module.create_plugin().id == manifest.id
        assert set(manifest.after) <= set(features)
    # 要排在谁后面，默认的启用顺序是照着做的
    for manifest in features.values():
        for earlier in manifest.after:
            assert enabled.index(earlier) < enabled.index(manifest.id)
    # 扩展点：查询和知识库各开一个，平台、站点插件的能力只能是这两个加上「对通用配置的补充」
    assert features["wiki_query"].extension_points == (EXTENSION_WIKI,)
    assert features["knowledge"].extension_points == (EXTENSION_KNOWLEDGE,)
    assert extension_points() == {EXTENSION_WIKI, EXTENSION_KNOWLEDGE}


def test_defaults_ship_with_the_package_and_a_deployment_only_writes_what_differs(
    tmp_path: Path,
) -> None:
    # 核心仓库自己的 config/ 是空的：全用默认值，不指向任何 wiki、不用站点插件
    assert sorted(p.name for p in (REPO / "config").glob("*.yaml")) == ["secrets.example.yaml"]
    config = load_config(REPO / "config", with_overrides=False)
    for name, path in config.sources.items():
        if name.startswith("plugin."):
            assert path.name == "config.yaml" and SOURCE / "plugins" in path.parents, name
        elif name.startswith("platform."):
            assert path.name == "config.yaml" and SOURCE / "platforms" in path.parents, name
        elif name.startswith("persona/"):
            assert SOURCE / "plugins" / "chat" / "configs" in path.parents  # 人设跟着对话插件
        else:
            assert path.parent == DEFAULTS_DIR, name
    assert {"app", "core.wiki", "core.plugins", "core.llm", "gateway.qq"} <= set(config.sources)
    wiki = config.get("core.wiki")
    assert wiki["wikis"] == [] and config.wikis == () and config.sites == []
    assert config.platforms == []
    plain, nothing = wiki_plugins("", "")  # 什么都不写：纯 MediaWiki，没有站点插件
    assert plain.id == DEFAULT_PLATFORM and nothing is None
    with pytest.raises(ValueError, match="不知道是哪个 wiki"):
        load_wikis(config)

    # 一个部署：只写它和默认值不一样的地方，别的照默认值
    deployment = tmp_path / "config"
    write_wikis(deployment, {"id": "main", "api_url": f"{SITE}/api.php"}, name="某站")
    (deployment / "app.yaml").write_text("log:\n  level: DEBUG\n", "utf-8")
    (deployment / "mine.yaml").write_text("anything: 1\n", "utf-8")  # 包里没有的配置，原样算数
    mine = load_config(deployment, with_overrides=False)
    assert mine.get("core.wiki")["name"] == "某站"
    assert mine.get("core.wiki")["user_agent"] == wiki["user_agent"]  # 没写的项还是默认值
    assert mine.app["log"] == {**config.app["log"], "level": "DEBUG"}
    assert mine.get("mine") == {"anything": 1}
    assert mine.get("core.llm") == config.get("core.llm")
    # 默认值写在哪：还是包里的那个文件（部署目录里的是对它的改动）
    assert mine.source_path("core.wiki") == DEFAULTS_DIR / "core.wiki.yaml"
    (only,) = load_wikis(mine)
    assert (only.id, only.label, only.site_base, only.user_agent) == (
        "main", "main", SITE, wiki["user_agent"],
    )  # fmt: skip
    assert only.page_url("某 页") == f"{SITE}/wiki/%E6%9F%90_%E9%A1%B5"
    assert mine.platforms == [DEFAULT_PLATFORM] and mine.sites == []


def test_the_plugin_host_only_loads_plugins_that_follow_the_spec() -> None:
    host = PluginHost(AppConfig(root=Path("."), files={"core.plugins": {"enabled": ["safety"]}},
                                secrets={}))  # fmt: skip
    host.load()
    (loaded,) = host.loaded
    assert loaded.id == "safety" and loaded.manifest.name == "安全兜底"

    missing = PluginHost(
        AppConfig(root=Path("."), files={"core.plugins": {"enabled": ["nope"]}}, secrets={})
    )
    with pytest.raises(PluginError, match="没有叫「nope」的功能插件"):
        missing.load()


# ---------- 别的包带来的功能插件 ----------

ECHO = """
class Echo:
    id = "{id}"

    def register(self, ctx):
        ctx.services["echo_prefix"] = ctx.config.plugin("echo")["prefix"]


def create_plugin():
    return Echo()
"""


def feature_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: str, plugin_id: str, source: str
) -> Path:
    directory = site_packages(tmp_path, monkeypatch, package)
    (directory / "__init__.py").write_text(source, encoding="utf-8")
    manifest = {"id": plugin_id, "kind": "feature", "name": "回声", "description": "原样说回去。"}
    (directory / "plugin.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), "utf-8")
    (directory / "config.yaml").write_text("prefix: '>'\n", "utf-8")
    return directory


def test_a_feature_plugin_from_another_package_is_found_once_it_is_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = "jianshanagent_plugin_echo"
    directory = feature_package(tmp_path, monkeypatch, package, "echo", ECHO.format(id="echo"))
    # 装在环境里但没登记：核心不知道有它
    assert "echo" not in feature_manifests()
    register_plugins(monkeypatch, features={"echo": package})
    manifest = feature_manifests()["echo"]
    assert (manifest.package, manifest.path, manifest.config_name) == (
        package, directory, "plugin.echo",
    )  # fmt: skip

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "core.plugins.yaml").write_text("enabled: [safety, echo]\n", "utf-8")
    config = load_config(tmp_path / "config", with_overrides=False)
    assert config.plugin("echo") == {"prefix": ">"}  # 默认配置在它自己的包里
    assert config.source_path("plugin.echo") == directory / "config.yaml"
    host = PluginHost(config)
    host.load()
    assert [p.id for p in host.loaded] == ["safety", "echo"]
    assert host.loaded[1].context.services["echo_prefix"] == ">"
    assert check_plugin(package).id == "echo"  # 规范自检也过


def test_registered_plugins_that_break_the_rules_are_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 登记的名字和清单里的 id 不一样
    feature_package(
        tmp_path, monkeypatch, "jianshanagent_plugin_echo", "echo", ECHO.format(id="echo")
    )
    register_plugins(monkeypatch, features={"shout": "jianshanagent_plugin_echo"})
    with pytest.raises(PluginError, match="id（echo）要和登记的名字（shout）一样"):
        feature_manifests()
    # 和核心自带的重名
    feature_package(
        tmp_path, monkeypatch, "jianshanagent_plugin_chat", "chat", ECHO.format(id="chat")
    )
    register_plugins(monkeypatch, features={"chat": "jianshanagent_plugin_chat"})
    with pytest.raises(PluginError, match="功能插件的 id「chat」重了"):
        feature_manifests()
    # 登记了一个没装的包
    register_plugins(monkeypatch, sites={"ghost": "jianshanagent_site_ghost"})
    with pytest.raises(PluginError, match="找不到站点插件「jianshanagent_site_ghost」"):
        installed_sites()

    # 名字不照规范取：规范自检说得出该叫什么
    feature_package(tmp_path, monkeypatch, "echo_for_jsa", "echo", ECHO.format(id="echo"))
    register_plugins(monkeypatch, features={"echo": "echo_for_jsa"})
    with pytest.raises(PluginError, match="包名「echo_for_jsa」不照规范"):
        check_plugin("echo_for_jsa")
    feature_package(
        tmp_path, monkeypatch, "jianshanagent_plugin_voice", "echo", ECHO.format(id="echo")
    )
    register_plugins(monkeypatch, features={"echo": "jianshanagent_plugin_voice"})
    with pytest.raises(PluginError, match="照命名规范应该叫「jianshanagent_plugin_echo」"):
        check_plugin("jianshanagent_plugin_voice")
    with pytest.raises(PluginError, match="没有登记"):
        check_plugin("jianshanagent_plugin_echo")


# ---------- 清单不合规范 ----------


def test_a_manifest_that_breaks_the_format_is_rejected_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sites = site_packages(tmp_path, monkeypatch, "bad_sites")

    def read(name: str) -> None:
        site(f"bad_sites.{name}")

    write_site(sites / "renamed", manifest={"id": "another"})
    with pytest.raises(PluginError, match="id（another）要和目录名（renamed）一样"):
        read("renamed")
    write_site(sites / "typo", manifest={"capabilites": ["wiki"]})  # 键名写错了
    with pytest.raises(PluginError, match="有不认识的项：capabilites"):
        read("typo")
    write_site(sites / "unknown", capabilities=("wiki", "search"))
    with pytest.raises(PluginError, match="不认识的能力：search（现在有 extend、knowledge、wiki）"):
        read("unknown")
    write_site(sites / "feature", manifest={"kind": "feature"})
    with pytest.raises(PluginError, match="kind 应该是 site"):
        read("feature")
    write_site(sites / "nameless", manifest={"name": ""})
    with pytest.raises(PluginError, match="没有写 name"):
        read("nameless")
    (sites / "empty").mkdir()
    (sites / "empty" / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(PluginError, match=r"没有清单（plugin\.yaml）"):
        read("empty")
    with pytest.raises(PluginError, match="找不到站点插件「bad_sites.gone」"):
        read("gone")
    # 写了一个核心里没有的平台
    write_site(sites / "nowhere_x", platform="nowhere")
    with pytest.raises(
        PluginError, match="platform 写的是「nowhere」，核心里没有这个平台（现在有 bwiki、huiji"
    ):
        read("nowhere_x")
    # 站的 id 里没带平台：两个平台上同一个题材的站就没法装在一起了
    write_site(sites / "other", platform="huiji")
    with pytest.raises(PluginError, match="id 要以平台的 id 开头：huiji_<这个站的名字>"):
        read("other")
    # id 带着平台，清单里却没写它是那个平台上的
    write_site(sites / "bwiki_lost")
    with pytest.raises(PluginError, match="以平台「bwiki」的 id 开头，清单里却没有写 platform"):
        read("bwiki_lost")
    # 以前的写法（平台也是插件，站点插件写上级）：说清楚现在怎么写
    write_site(sites / "huiji_old", manifest={"parent": "huiji"})
    with pytest.raises(PluginError, match="parent 不用了：.*改成 platform: huiji"):
        read("huiji_old")
    # 功能插件的清单是另一套键
    write_site(sites / "mixed", manifest={"kind": "feature", "platform": "x", "capabilities": []})
    with pytest.raises(PluginError, match="有不认识的项"):
        read_manifest(sites / "mixed", "bad_sites.mixed", KIND_FEATURE)


# ---------- 站点插件和它的平台，接扩展点 ----------

STATION = """
from jianshanagent.platforms.huiji.wiki import HuijiData
from jianshanagent.plugins.wiki_query.provider import WikiProvider


class Station(WikiProvider):
    id = "huiji_station"
    kinds = {"hero": "英雄"}

    def __init__(self, data, label):
        self.data, self.label = data, label


def create(setup):
    # 平台在查询这个扩展点上的实例：读灰机的 Data 用
    return Station(setup.platform(HuijiData), setup.raw.get("label"))
"""


def station(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个独立的包：灰机上的一个站（``huiji_station``，``platform: huiji``）。"""
    return write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_huiji_station"),
        manifest={"id": "huiji_station", "description": "灰机上的一个站。"},
        platform="huiji",
        capabilities=("wiki", "extend"),
        config={"label": "某个站", "extend": {"plugin.wiki_query": {"format": {"max_chars": 500}}}},
        wiki=STATION,
    )


def test_a_registered_site_is_named_by_its_id_and_brings_its_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = station(tmp_path, monkeypatch)
    register_plugins(monkeypatch, sites={"huiji_station": "jianshanagent_site_huiji_station"})
    assert list(installed_sites()) == ["huiji_station"]
    platform, mine = wiki_plugins("", "huiji_station")
    assert mine is installed_sites()["huiji_station"] and mine.platform == "huiji"
    assert platform is platforms()["huiji"]  # 平台跟着站点插件走
    assert wiki_plugins("huiji", "huiji_station") == (platform, mine)  # 两样都写也行，得对得上
    with pytest.raises(PluginError, match="是「huiji」平台上的站，platform 却写的是「bwiki」"):
        wiki_plugins("bwiki", "huiji_station")

    write_wikis(tmp_path / "config", *one_wiki("huiji_station")["wikis"])
    config = load_config(tmp_path / "config", with_overrides=False)
    assert (config.platforms, config.sites) == (["huiji"], ["huiji_station"])
    assert [m.id for m in config.wiki("main").chain] == ["huiji", "huiji_station"]
    assert config.site("huiji_station")["label"] == "某个站"
    assert config.source_path("site.huiji_station") == directory / "config.yaml"
    assert config.source_path("platform.huiji") == SOURCE / "platforms" / "huiji" / "config.yaml"
    assert config.plugin("wiki_query")["format"]["max_chars"] == 500  # 站的补充并进去了
    assert "kb-ignore" in config.plugin("knowledge")["clean"]["remove_classes"]  # 平台的也在

    data, loaded = load_sites(config, EXTENSION_WIKI, WIKI)
    assert isinstance(data, HuijiData) and isinstance(loaded, WikiProvider)
    assert loaded.data is data and loaded.label == "某个站"  # type: ignore[attr-defined]
    # 知识库这个扩展点上只有平台：这个站没接
    (knowledge,) = load_sites(config, EXTENSION_KNOWLEDGE, WIKI)
    assert isinstance(knowledge, HuijiKnowledge) and knowledge.data_namespace == 3500
    # 包名照规范：前缀 + id（站的 id 里带着平台）
    assert check_plugin("jianshanagent_site_huiji_station").platform == "huiji"

    # 没有站点插件的 wiki：写它在哪个平台上，只加载平台的那一份
    write_wikis(tmp_path / "config", {"id": "main", "api_url": f"{SITE}/api.php",
                                      "platform": "huiji"})  # fmt: skip
    bare = load_config(tmp_path / "config", with_overrides=False)
    assert (bare.platforms, bare.sites) == (["huiji"], [])
    assert [type(p) for p in load_sites(bare, EXTENSION_WIKI, WIKI)] == [HuijiData]
    assert "kb-ignore" in bare.plugin("knowledge")["clean"]["remove_classes"]
    assert bare.plugin("wiki_query")["format"]["max_chars"] != 500
    write_wikis(tmp_path / "config", {"id": "main", "api_url": f"{SITE}/api.php",
                                      "platform": "wikia"})  # fmt: skip
    with pytest.raises(PluginError, match="核心里没有叫「wikia」的平台（现在有 bwiki、huiji"):
        load_config(tmp_path / "config", with_overrides=False)

    # 没登记的包也能用，写包路径
    their = site_packages(tmp_path, monkeypatch, "their_sites")
    extra = write_site(their / "huiji_other", platform="huiji")
    assert [m.id for m in wiki_plugins("", "their_sites.huiji_other")] == ["huiji", "huiji_other"]
    assert extra.name == "huiji_other"
    with pytest.raises(PluginError, match="没有叫「nowhere」的站点插件（装着的有：huiji_station"):
        site("nowhere")

    # 包名和 id 对不上：规范自检指出该叫什么
    write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_lonely"),
        manifest={"id": "huiji_lonely", "description": "x"},
        platform="huiji",
    )
    register_plugins(monkeypatch, sites={"huiji_lonely": "jianshanagent_site_lonely"})
    with pytest.raises(PluginError, match="照命名规范应该叫「jianshanagent_site_huiji_lonely」"):
        check_plugin("jianshanagent_site_lonely")


def test_a_site_on_no_platform_is_plain_mediawiki(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = "class P:\n    id = 'thwiki'\n\n\ndef create(setup):\n    return P()\n"
    write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_thwiki"),
        manifest={"id": "thwiki", "description": "自己架的站。"},
        capabilities=("wiki",),
        wiki=ok,
    )
    register_plugins(monkeypatch, sites={"thwiki": "jianshanagent_site_thwiki"})
    platform, mine = wiki_plugins("", "thwiki")
    assert (platform.id, mine.platform if mine else "") == (DEFAULT_PLATFORM, DEFAULT_PLATFORM)
    config = config_for("thwiki")
    assert (config.platforms, config.sites) == ([DEFAULT_PLATFORM], ["thwiki"])
    (loaded,) = load_sites(config, EXTENSION_WIKI, WIKI)  # 平台什么都不多带，只有它自己
    assert loaded.id == "thwiki"
    assert check_plugin("jianshanagent_site_thwiki").id == "thwiki"


def test_an_extension_point_that_cannot_be_wired_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sites = site_packages(tmp_path, monkeypatch, "wired_sites")
    (sites / "base.py").write_text("class Data:\n    pass\n", "utf-8")
    ok = "class P:\n    id = '{id}'\n\n\ndef create(setup):\n    return P()\n"

    def load(name: str, extension: str = EXTENSION_WIKI) -> list[Any]:
        return load_sites(config_for(f"wired_sites.{name}"), extension, WIKI)

    # 清单里写了，包里没有那个模块
    write_site(sites / "hollow", capabilities=("wiki",))
    with pytest.raises(PluginError, match="清单里写了 wiki，包里却没有这个模块"):
        load("hollow")
    # 模块在，没有入口
    write_site(sites / "noentry", capabilities=("wiki",), wiki="x = 1\n")
    with pytest.raises(PluginError, match=r"里没有 create\(setup\)"):
        load("noentry")
    # 入口返回的对象 id 和清单不一样
    write_site(sites / "wrongid", capabilities=("wiki",), wiki=ok.format(id="someone"))
    with pytest.raises(PluginError, match="id 是「someone」，要和清单里的 id 一样"):
        load("wrongid")
    # 清单里没写的扩展点不加载（哪怕模块在）
    write_site(sites / "undeclared", wiki=ok.format(id="undeclared"))
    assert load("undeclared") == []
    # 模块里用到的别的东西没装：原样报出来，不说成「没有这个模块」
    write_site(sites / "broken", capabilities=("wiki",), wiki="import not_installed_anywhere\n")
    with pytest.raises(ModuleNotFoundError, match="not_installed_anywhere"):
        load("broken")

    # 站点插件要用平台的东西，平台却没接这个扩展点（BWiki 不管知识库）：
    # 统一在 setup.platform 这里报错
    wants = (
        "from wired_sites.base import Data\n\n\n"
        "def create(setup):\n    return setup.platform(Data)\n"
    )
    write_site(sites / "bwiki_leaf", platform="bwiki", capabilities=("knowledge",), knowledge=wants)
    with pytest.raises(
        PluginError, match="站点插件「bwiki_leaf」要用平台的 Data，但没找到：它清单里写的平台是"
    ):
        load("bwiki_leaf", EXTENSION_KNOWLEDGE)
    # 平台在这个扩展点上有东西，但不是要的那个类
    write_site(sites / "huiji_leaf", platform="huiji", capabilities=("wiki",), wiki=wants)
    with pytest.raises(PluginError, match="「huiji_leaf」要用平台的 Data，但没找到"):
        load("huiji_leaf")

    setup = SiteSetup(
        manifest=site("wired_sites.undeclared"), config=config_for(""), raw={}, wiki=WIKI
    )
    assert setup.id == "undeclared" and setup.above is None


def test_instance_local_sites_and_plugins_load_without_packaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实例目录下的 sites/<id>/ 和 plugins/<id>/ 免打包直接加载，随实例仓库同步。"""
    monkeypatch.syspath_prepend(str(tmp_path))

    # 1. 实例目录下直接放 sites/huiji_mysite/（灰机上的站：平台是核心带着的）
    write_site(
        tmp_path / "sites" / "huiji_mysite",
        manifest={"id": "huiji_mysite", "description": "实例目录内的站点插件。"},
        platform="huiji",
        capabilities=("wiki", "extend"),
        config={"label": "自写站", "extend": {"plugin.wiki_query": {"format": {"max_chars": 600}}}},
        wiki=STATION.replace("huiji_station", "huiji_mysite"),
    )

    # 2. 实例目录下直接放 plugins/hello/
    hello = tmp_path / "plugins" / "hello"
    hello.mkdir(parents=True)
    (hello / "plugin.yaml").write_text(
        "id: hello\nkind: feature\nname: 打招呼\ndescription: 实例内功能插件\n", "utf-8"
    )
    (hello / "config.yaml").write_text("word: 你好\n", "utf-8")
    (hello / "__init__.py").write_text(
        "class Hello:\n"
        "    id = 'hello'\n"
        "    def register(self, ctx):\n"
        "        ctx.register_service('hello_word', ctx.config.plugin('hello')['word'])\n\n"
        "def create_plugin():\n"
        "    return Hello()\n",
        "utf-8",
    )

    cfg_dir = tmp_path / "config"
    write_wikis(cfg_dir, *one_wiki("huiji_mysite")["wikis"])
    (cfg_dir / "core.plugins.yaml").write_text(yaml.safe_dump({"enabled": ["hello"]}), "utf-8")

    config = load_config(cfg_dir, with_overrides=False)
    assert (config.platforms, config.sites) == (["huiji"], ["huiji_mysite"])
    assert config.plugin("wiki_query")["format"]["max_chars"] == 600
    assert config.plugin("hello") == {"word": "你好"}

    _, loaded_site = load_sites(config, EXTENSION_WIKI, WIKI)
    assert loaded_site.id == "huiji_mysite" and loaded_site.label == "自写站"

    host = PluginHost(config)
    host.load()
    assert host.services["hello_word"] == "你好"
