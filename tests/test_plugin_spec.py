"""插件规范（``core/manifest.py``、``core/sites.py``，说明在 ``docs/插件规范.md``）。

- 核心自带的功能插件都合规范：有清单，清单说的和代码里有的对得上，配置在插件自己的目录里；
- 核心不带站点插件。别的包登记了（entry points）就能按 id 用；站点插件是一棵树，配置里只写最末一级，
  上级顺着清单带出来；
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
    KIND_FEATURE,
    PluginError,
    extension_points,
    feature_manifests,
    installed_sites,
    read_manifest,
    site_chain,
)
from jianshanagent.core.plugin import PluginHost
from jianshanagent.core.sites import EXTENSION_KNOWLEDGE, EXTENSION_WIKI, SiteSetup, load_sites
from jianshanagent.core.wiki import WikiSite
from jianshanagent.plugins.wiki_query.provider import WikiProvider
from jianshanagent.testing.conformance import check_plugin
from jianshanagent.testing.sites import register_plugins, site_packages, write_site

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "jianshanagent"
SITE = "https://wiki.example"
WIKI = WikiSite(api_url=f"{SITE}/api.php", site_base=SITE)


def config_for(site: str, **files: dict[str, Any]) -> AppConfig:
    return AppConfig(root=Path("."), files={"core.wiki": {"site": site}, **files}, secrets={})


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
    # 扩展点：查询和知识库各开一个，站点插件的能力只能是这两个加上「对通用配置的补充」
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
        elif name.startswith("persona/"):
            assert SOURCE / "plugins" / "chat" / "configs" in path.parents  # 人设跟着对话插件
        else:
            assert path.parent == DEFAULTS_DIR, name
    assert {"app", "core.wiki", "core.plugins", "core.llm", "gateway.qq"} <= set(config.sources)
    wiki = config.get("core.wiki")
    assert (wiki["api_url"], wiki["site"]) == ("", "") and config.sites == []
    assert site_chain("") == ()

    # 一个部署：只写它和默认值不一样的地方，别的照默认值
    deployment = tmp_path / "config"
    deployment.mkdir()
    (deployment / "core.wiki.yaml").write_text(
        f'name: "某站"\napi_url: "{SITE}/api.php"\n', "utf-8"
    )
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

    def chain_of(name: str) -> None:
        site_chain(f"bad_sites.{name}")

    write_site(sites / "renamed", manifest={"id": "another"})
    with pytest.raises(PluginError, match="id（another）要和目录名（renamed）一样"):
        chain_of("renamed")
    write_site(sites / "typo", manifest={"capabilites": ["wiki"]})  # 键名写错了
    with pytest.raises(PluginError, match="有不认识的项：capabilites"):
        chain_of("typo")
    write_site(sites / "unknown", capabilities=("wiki", "search"))
    with pytest.raises(PluginError, match="不认识的能力：search（现在有 extend、knowledge、wiki）"):
        chain_of("unknown")
    write_site(sites / "feature", manifest={"kind": "feature"})
    with pytest.raises(PluginError, match="kind 应该是 site"):
        chain_of("feature")
    write_site(sites / "nameless", manifest={"name": ""})
    with pytest.raises(PluginError, match="没有写 name"):
        chain_of("nameless")
    (sites / "empty").mkdir()
    (sites / "empty" / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(PluginError, match="没有插件清单（plugin.yaml）"):
        chain_of("empty")
    with pytest.raises(PluginError, match="找不到站点插件「bad_sites.gone」"):
        chain_of("gone")
    # 上级写了一个不存在的插件
    write_site(sites / "orphan", parent="nobody")
    with pytest.raises(PluginError, match="没有叫「nobody」的站点插件"):
        chain_of("orphan")
    # 上级绕成了圈
    write_site(sites / "ping", parent="bad_sites.pong")
    write_site(sites / "pong", parent="bad_sites.ping")
    with pytest.raises(PluginError, match="绕成了圈"):
        chain_of("ping")
    # 功能插件的清单是另一套键
    write_site(sites / "mixed", manifest={"kind": "feature", "parent": "x", "capabilities": []})
    with pytest.raises(PluginError, match="有不认识的项"):
        read_manifest(sites / "mixed", "bad_sites.mixed", KIND_FEATURE)


# ---------- 站点插件的树，和接扩展点 ----------

PLATFORM = """
from jianshanagent.plugins.wiki_query.provider import WikiProvider


class PlatData(WikiProvider):
    id = "plat"

    def __init__(self, namespace):
        self.namespace = namespace


def create(setup):
    return PlatData(setup.raw["data_namespace"])
"""

STATION = """
from jianshanagent.plugins.wiki_query.provider import WikiProvider
from jianshanagent_site_plat.wiki import PlatData


class Station(WikiProvider):
    id = "station"
    kinds = {"hero": "英雄"}

    def __init__(self, data, label):
        self.data, self.label = data, label


def create(setup):
    # 上级（平台）在查询这个扩展点上的实例：读平台的数据用
    return Station(setup.parent(PlatData), setup.raw.get("label"))
"""


def platform_and_station(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """两个独立的包：一个平台，和这个平台上的一个站（清单里 ``parent: plat``）。"""
    plat = write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_plat"),
        manifest={"id": "plat", "description": "一个平台。"},
        capabilities=("wiki", "extend"),
        config={
            "data_namespace": 3500,
            "extend": {"plugin.knowledge": {"clean": {"remove_classes": ["plat-ignore"]}}},
        },
        wiki=PLATFORM,
    )
    station = write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_plat_station"),
        manifest={"id": "station", "description": "平台上的一个站。"},
        parent="plat",
        capabilities=("wiki", "extend"),
        config={"label": "某个站", "extend": {"plugin.wiki_query": {"format": {"max_chars": 500}}}},
        wiki=STATION,
    )
    return plat, station


def test_a_registered_site_is_named_by_its_id_and_brings_its_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plat, station = platform_and_station(tmp_path, monkeypatch)
    register_plugins(
        monkeypatch,
        sites={"plat": "jianshanagent_site_plat", "station": "jianshanagent_site_plat_station"},
    )
    assert list(installed_sites()) == ["plat", "station"]
    chain = site_chain("station")
    assert [m.id for m in chain] == ["plat", "station"] and chain[0] is installed_sites()["plat"]
    assert [m.id for m in site_chain("plat")] == ["plat"]

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "core.wiki.yaml").write_text(
        yaml.safe_dump({"api_url": f"{SITE}/api.php", "site": "station"}), "utf-8"
    )
    config = load_config(tmp_path / "config", with_overrides=False)
    assert config.sites == ["plat", "station"] and config.site("station")["label"] == "某个站"
    assert config.source_path("site.station") == station / "config.yaml"
    assert config.source_path("site.plat") == plat / "config.yaml"
    assert config.plugin("wiki_query")["format"]["max_chars"] == 500  # 站的补充并进去了
    assert "plat-ignore" in config.plugin("knowledge")["clean"]["remove_classes"]  # 平台的也在

    platform, mine = load_sites(config, EXTENSION_WIKI, WIKI)
    assert isinstance(mine, WikiProvider) and mine.data is platform  # type: ignore[attr-defined]
    assert platform.namespace == 3500 and mine.label == "某个站"  # type: ignore[attr-defined]
    # 它们都没接知识库这个扩展点
    assert load_sites(config, EXTENSION_KNOWLEDGE, WIKI) == []
    # 两个包的名字都照规范：前缀 + 从平台到站的 id
    assert check_plugin("jianshanagent_site_plat").id == "plat"
    assert check_plugin("jianshanagent_site_plat_station").parent == "plat"

    # 没登记的包也能用，写包路径；它的上级可以是登记了的插件
    extra = write_site(site_packages(tmp_path, monkeypatch, "their_sites") / "other", parent="plat")
    assert [m.id for m in site_chain("their_sites.other")] == ["plat", "other"]
    assert extra.name == "other"
    with pytest.raises(PluginError, match="没有叫「nowhere」的站点插件（装着的有：plat、station"):
        site_chain("nowhere")

    # 站的包名里漏了平台：规范自检指出该叫什么
    write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_lonely"),
        manifest={"id": "lonely", "description": "x"},
        parent="plat",
    )
    register_plugins(
        monkeypatch,
        sites={"plat": "jianshanagent_site_plat", "lonely": "jianshanagent_site_lonely"},
    )
    with pytest.raises(PluginError, match="照命名规范应该叫「jianshanagent_site_plat_lonely」"):
        check_plugin("jianshanagent_site_lonely")


def test_an_extension_point_that_cannot_be_wired_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sites = site_packages(tmp_path, monkeypatch, "wired_sites")
    (sites / "base.py").write_text("class Data:\n    pass\n", "utf-8")
    ok = "class P:\n    id = '{id}'\n\n\ndef create(setup):\n    return P()\n"

    def load(name: str) -> list[Any]:
        return load_sites(config_for(f"wired_sites.{name}"), EXTENSION_WIKI, WIKI)

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

    # 下级要用上级的东西，上级却没接这个扩展点：统一在 setup.parent 这里报错
    top = write_site(sites / "top")
    child = (
        "from wired_sites.base import Data\n\n\ndef create(setup):\n    return setup.parent(Data)\n"
    )
    write_site(top / "sites" / "leaf", parent="top", capabilities=("wiki",), wiki=child)
    with pytest.raises(PluginError, match="「leaf」要用它上级的 Data，但没找到"):
        load("top.sites.leaf")
    # 放在上级的 sites/ 里，清单里的上级却写的是别人
    write_site(top / "sites" / "stray", parent="elsewhere")
    with pytest.raises(PluginError, match="它放在「top」下面，清单里应该parent: top"):
        site_chain("wired_sites.top.sites.stray")

    setup = SiteSetup(
        manifest=site_chain("wired_sites.top")[0], config=config_for(""), raw={}, wiki=WIKI,
        parents={},
    )  # fmt: skip
    assert setup.id == "top"


def test_instance_local_sites_and_plugins_load_without_packaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实例目录下的 sites/<id>/ 和 plugins/<id>/ 免打包直接加载，随实例仓库同步。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    plat, _ = platform_and_station(tmp_path / "pkgs", monkeypatch)
    register_plugins(monkeypatch, sites={"plat": "jianshanagent_site_plat"})

    # 1. 实例目录下直接放 sites/mysite/（上级指向已安装的平台插件 plat）
    write_site(
        tmp_path / "sites" / "mysite",
        manifest={"id": "mysite", "description": "实例目录内的站点插件。"},
        parent="plat",
        capabilities=("wiki", "extend"),
        config={"label": "自写站", "extend": {"plugin.wiki_query": {"format": {"max_chars": 600}}}},
        wiki=STATION.replace("station", "mysite"),
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
    cfg_dir.mkdir()
    (cfg_dir / "core.wiki.yaml").write_text(
        yaml.safe_dump({"api_url": f"{SITE}/api.php", "site": "mysite"}), "utf-8"
    )
    (cfg_dir / "core.plugins.yaml").write_text(yaml.safe_dump({"enabled": ["hello"]}), "utf-8")

    config = load_config(cfg_dir, with_overrides=False)
    assert config.sites == ["plat", "mysite"]
    assert config.plugin("wiki_query")["format"]["max_chars"] == 600
    assert config.plugin("hello") == {"word": "你好"}

    _, loaded_site = load_sites(config, EXTENSION_WIKI, WIKI)
    assert loaded_site.id == "mysite" and loaded_site.label == "自写站"

    host = PluginHost(config)
    host.load()
    assert host.services["hello_word"] == "你好"
    assert plat.name == "jianshanagent_site_plat"
