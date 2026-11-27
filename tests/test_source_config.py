"""通用来源契约回归；不连接真实 Wiki、微博、数据库或模型。"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from jianshanagent.core import manifest, sources
from jianshanagent.core.config import (
    AppConfig,
    is_locked,
    load_config,
    locked_changes,
    restore_override,
    set_wiki_mounted,
    wiki_entries,
    write_override,
    write_source_instances,
)
from jianshanagent.core.manifest import KIND_PLATFORM, PluginError, read_manifest
from jianshanagent.core.sources import (
    SourceSetup,
    disabled_source_ids,
    load_source_adapter,
    retained_source_ids,
    source_instance,
    source_instances,
    source_labels,
)


def _config(tmp_path: Path, *, wikis: bool = True, rows: object = None) -> AppConfig:
    root = tmp_path / "config"
    root.mkdir()
    (root / "core.wiki.yaml").write_text(
        yaml.safe_dump(
            {"wikis": [{"id": "main", "label": "原Wiki", "platform": "mediawiki"}] if wikis else []}
        ),
        encoding="utf-8",
    )
    (root / "core.sources.yaml").write_text(
        yaml.safe_dump({"sources": [] if rows is None else rows}), encoding="utf-8"
    )
    return load_config(root, overrides_dir=tmp_path / "overrides")


def _row(source_id: str = "posts", **extra: object) -> dict[str, object]:
    return {"id": source_id, "label": "微博资料", "platform": "weibo", **extra}


def test_sources_keep_legacy_config_mapping_and_wiki_view(tmp_path: Path) -> None:
    config = _config(tmp_path, rows=[_row()])
    assert isinstance(config.sources, dict)
    assert config.sources["core.sources"].is_file()
    assert [source.id for source in source_instances(config)] == ["main", "posts"]
    assert config.for_source("main") is config.for_wiki("main")
    assert config.for_source("posts") is config
    assert config.wikis[0].id == "main"
    assert config.source("main").is_wiki
    assert not config.source("posts").is_wiki
    assert config.source("posts").role == "unknown"
    assert source_labels(config) == {"main": "原Wiki", "posts": "微博资料"}
    assert retained_source_ids(config) == {"main", "posts"}
    assert "" not in retained_source_ids(config)
    with pytest.raises(ValueError, match="没有登记"):
        source_instance(config, "missing")


def test_wiki_disabled_projection_and_documents_retained(tmp_path: Path) -> None:
    config = _config(tmp_path, rows=[_row()])
    set_wiki_mounted(config, "main", False)
    write_source_instances(config, [_row(mounted=False)])
    loaded = load_config(config.root, config.overrides_dir)
    assert disabled_source_ids(loaded) == {"main", "posts"}
    assert retained_source_ids(loaded) == {"main", "posts"}
    assert loaded.source("main").mounted is False
    assert loaded.wikis[0].id == "main"
    assert source_labels(loaded)["posts"] == "微博资料"
    with pytest.raises(PluginError, match="已停用"):
        load_source_adapter(loaded, "posts")
    write_source_instances(loaded, [_row(mounted=True)])
    restored = load_config(config.root, config.overrides_dir)
    assert disabled_source_ids(restored) == {"main"}


@pytest.mark.parametrize(
    "rows, message",
    [
        ([_row("main")], "重合"),
        ([_row(), _row()], "重复"),
        ([_row("../bad")], "id"),
        ([_row(platform="mediawiki")], "core.wiki"),
        ([_row(platform="untrusted.module")], "受信任"),
        ([_row(driver="documents")], "不允许"),
        ([_row(package="arbitrary.module")], "不允许"),
        ([_row(api_url="https://example.invalid")], "不允许"),
        ([_row(role="verified")], "role"),
        ([_row(role=["official"])], "role"),
        ([_row(mounted="false")], "mounted"),
        ([_row(label=" ")], "label"),
        ({"id": "posts"}, "列表"),
        (None, "列表"),
    ],
)
def test_reject_invalid_instances(rows: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        sources.document_source_instances(rows, wiki_ids={"main"})


def test_documents_platform_is_rejected_from_wiki_config() -> None:
    with pytest.raises(ValueError, match="documents"):
        wiki_entries({"wikis": [{"id": "bad", "platform": "weibo"}]})


def test_platform_driver_default_and_documents_capability() -> None:
    known = manifest.platforms()
    assert known["weibo"].driver == "documents"
    assert "documents" in known["weibo"].capabilities
    assert "documents" in manifest.extension_points()
    assert all(known[name].driver == "mediawiki" for name in ("mediawiki", "huiji", "bwiki"))


def test_manifest_rejects_unknown_driver(tmp_path: Path) -> None:
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump({"id": tmp_path.name, "name": "假平台", "driver": "arbitrary"}),
        encoding="utf-8",
    )
    with pytest.raises(PluginError, match="driver"):
        read_manifest(tmp_path, "trusted.fake", KIND_PLATFORM)


def test_specialized_source_write_reload_and_restore(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert is_locked("core.sources", ("sources",))
    assert locked_changes("core.sources", {"sources": [_row()]}) == ["sources"]
    with pytest.raises(ValueError, match="专用来源管理"):
        write_override(config, "core.sources", {"sources": [_row()]})
    write_source_instances(config, [_row(role="community")])
    path = config.override_path("core.sources")
    assert path is not None
    previous = path.read_text(encoding="utf-8")
    loaded = load_config(config.root, config.overrides_dir)
    assert loaded.source("posts").role == "community"
    write_source_instances(loaded, [_row(role="official", mounted=False)])
    assert not load_config(config.root, config.overrides_dir).source("posts").mounted
    restore_override(config, "core.sources", previous)
    assert load_config(config.root, config.overrides_dir).source("posts").role == "community"
    with pytest.raises(ValueError, match="不能删除"):
        write_source_instances(loaded, [])
    with pytest.raises(ValueError, match="重合"):
        write_source_instances(loaded, [_row("main")])
    restore_override(config, "core.sources", None)
    restored = load_config(config.root, config.overrides_dir)
    assert [source.id for source in restored.source_instances] == ["main"]


@pytest.mark.parametrize("raw", ["sources: [", "sources: null", "- not-a-mapping", "", "{}"])
def test_bad_source_override_fails_closed(tmp_path: Path, raw: str) -> None:
    config = _config(tmp_path, rows=[_row()])
    path = config.override_path("core.sources")
    assert path is not None
    path.parent.mkdir()
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="来源|sources"):
        load_config(config.root, config.overrides_dir)
    # 已装配的旧视图保持可用，不因一次 reload 失败丢掉存量来源登记。
    assert retained_source_ids(config) == {"main", "posts"}
    assert retained_source_ids(config.for_wiki("main")) == {"main", "posts"}


def test_conflicting_source_override_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path, rows=[_row()])
    path = config.override_path("core.sources")
    assert path is not None
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({"sources": [_row("main")]}), encoding="utf-8")
    with pytest.raises(ValueError, match="重合"):
        load_config(config.root, config.overrides_dir)


def test_unreadable_source_override_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jianshanagent.core import config as config_module

    config = _config(tmp_path, rows=[_row()])
    path = config.override_path("core.sources")
    assert path is not None
    path.parent.mkdir()
    path.write_text("sources: []", encoding="utf-8")
    original = config_module._load_yaml

    def unreadable(candidate: Path) -> dict[str, object]:
        if candidate == path:
            raise OSError("模拟来源登记读取失败")
        return original(candidate)

    monkeypatch.setattr(config_module, "_load_yaml", unreadable)
    with pytest.raises(ValueError, match="读取失败"):
        load_config(config.root, config.overrides_dir)
    assert retained_source_ids(config) == {"main", "posts"}
    config.override_errors["core.sources"] = "模拟曾被跳过的覆写错误"
    with pytest.raises(ValueError, match="覆写无效"):
        source_instances(config)


def test_source_registry_is_shared_by_all_wiki_views(tmp_path: Path) -> None:
    config = _config(tmp_path, rows=[_row()])
    wiki_path = config.root / "core.wiki.yaml"
    wiki_path.write_text(
        yaml.safe_dump({"wikis": [{"id": "main"}, {"id": "second", "platform": "bwiki"}]}),
        encoding="utf-8",
    )
    write_source_instances(config, [_row(mounted=False)])
    loaded = load_config(config.root, config.overrides_dir)
    assert disabled_source_ids(loaded.for_wiki("second")) == {"posts"}
    assert retained_source_ids(loaded.for_wiki("second")) == {"main", "second", "posts"}


def test_invalid_deployment_is_rejected_at_parse_time(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="重合"):
        _config(tmp_path, rows=[_row("main")])


def test_zero_wiki_trusted_adapter_requires_no_wiki_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 模拟未来核心安装的可信平台。模块从清单取，不从来源配置取。
    trusted = replace(manifest.platforms()["weibo"], package="trusted_source_test")
    monkeypatch.setattr(sources, "platforms", lambda: {"weibo": trusted})
    calls: list[SourceSetup] = []
    module = ModuleType("trusted_source_test")

    class Adapter:
        def normalize(self, rows: object) -> list[object]:
            return []

    def factory(setup: SourceSetup) -> Adapter:
        calls.append(setup)
        return Adapter()

    module.create_source_adapter = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "trusted_source_test", module)

    def forbid(*args: object, **kwargs: object) -> None:
        pytest.fail("文档来源不应构造 WikiSite")

    monkeypatch.setattr("jianshanagent.core.wiki.WikiSite.__init__", forbid)
    config = _config(tmp_path, wikis=False, rows=[_row()])
    assert not config.wikis
    assert not config.platforms
    adapter = load_source_adapter(config, "posts")
    assert adapter.normalize([]) == []
    assert calls[0].config is config
    assert calls[0].source.id == "posts"
    assert source_labels(config) == {"posts": "微博资料"}
    assert retained_source_ids(config) == {"posts"}


def test_adapter_contract_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, rows=[_row()])
    with pytest.raises(PluginError, match="Wiki"):
        load_source_adapter(config, "main")
    module = ModuleType("trusted_bad_test")
    trusted = replace(manifest.platforms()["weibo"], package=module.__name__)
    monkeypatch.setattr(sources, "platforms", lambda: {"weibo": trusted})
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(PluginError, match="create_source_adapter"):
        load_source_adapter(config, "posts")
    module.create_source_adapter = lambda setup: object()  # type: ignore[attr-defined]
    with pytest.raises(PluginError, match="normalize"):
        load_source_adapter(config, "posts")


@pytest.mark.parametrize(
    "value",
    ["5896401674", " https://weibo.com/u/5896401674/ ", "https://www.weibo.com/u/5896401674"],
)
def test_weibo_uid_canonical(value: str) -> None:
    from jianshanagent.weibo_collector_config import parse_weibo_uid

    assert parse_weibo_uid(value) == "5896401674"


@pytest.mark.parametrize(
    "value",
    [
        None,
        5896401674,
        True,
        "",
        "0",
        "0123",
        "１２３",
        "https://weibo.com/5896401674",
        "http://weibo.com/u/5896401674",
        "https://weibo.com.evil/u/123",
        "https://evil/u/123",
        "https://weibo.com:443/u/123",
        "https://user:token@weibo.com/u/123",
        "https://weibo.com/u/123?cookie=secret",
        "https://weibo.com/u/123#fragment",
        "https://weibo.com/u/123/extra",
        "https://wei\nbo.com/u/123",
        "https://weibo.com/u/%31%32%33",
        "file:///tmp/cookie",
    ],
)
def test_weibo_uid_rejects_untrusted_input(value: object) -> None:
    from jianshanagent.weibo_collector_config import parse_weibo_uid

    with pytest.raises(ValueError):
        parse_weibo_uid(value)


@pytest.mark.parametrize(
    "extra",
    [
        {"cookie": "secret"},
        {"proxy": "http://localhost"},
        {"url": "https://evil"},
        {"path": "/tmp"},
        {"package": "evil.module"},
        {"enabled": 1},
        {"include_reposts": "false"},
        {"initial_days": True},
        {"initial_days": 0},
        {"initial_days": 366},
        {"overlap_days": 0},
        {"overlap_days": 31},
        {"max_posts": 19},
        {"max_posts": 501},
        {"interval_seconds": 899},
        {"interval_seconds": 86401},
        {"max_posts": 20.0},
    ],
)
def test_weibo_collector_strict_fields(extra: dict[str, object]) -> None:
    from jianshanagent.weibo_collector_config import parse_weibo_collector

    with pytest.raises(ValueError):
        parse_weibo_collector({"uid": "5896401674", **extra})
    with pytest.raises(ValueError):
        parse_weibo_collector({})


def test_weibo_collector_canonical_raw_and_persisted_defaults(tmp_path: Path) -> None:
    config = _config(
        tmp_path, rows=[_row(role="official", collector={"uid": "https://weibo.com/u/5896401674"})]
    )
    settings = config.source("posts").raw["collector"]
    assert settings == {
        "uid": "5896401674",
        "enabled": False,
        "initial_days": 30,
        "overlap_days": 7,
        "max_posts": 200,
        "interval_seconds": 1800,
        "include_reposts": True,
    }
    write_source_instances(config, [_row(role="official", collector={"uid": settings["uid"]})])
    loaded = load_config(config.root, config.overrides_dir)
    assert loaded.source("posts").raw["collector"] == settings
    assert loaded.source("posts").role == "official"
    path = config.override_path("core.sources")
    assert path is not None
    assert yaml.safe_load(path.read_text())["sources"][0]["collector"] == settings
    for collector in (None, {"uid": "12345"}):
        row = _row() if collector is None else _row(collector=collector)
        with pytest.raises(ValueError, match="UID"):
            write_source_instances(loaded, [row])
    assert (
        load_config(config.root, config.overrides_dir).source("posts").raw["collector"] == settings
    )


@pytest.mark.parametrize("author", ["5896401674", "1111", "", None])
def test_weibo_first_binding_requires_matching_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    author: str | None,
) -> None:
    root = tmp_path / "kb"
    monkeypatch.setenv("JIANSHANAGENT_KB_DIR", str(root))
    config = _config(tmp_path, rows=[_row()])
    directory = root / "posts" / "source-import"
    stage = directory / "generations" / ("a" * 32)
    stage.mkdir(parents=True)
    (directory / "current.json").write_text(json.dumps({"generation": "a" * 32}))
    (stage / "documents.json").write_text(json.dumps({"rows": [{"author_id": author}]}))
    if author == "5896401674":
        write_source_instances(config, [_row(collector={"uid": author})])
        assert (
            load_config(config.root, config.overrides_dir).source("posts").raw["collector"]["uid"]
            == author
        )
    else:
        with pytest.raises(ValueError, match="作者"):
            write_source_instances(config, [_row(collector={"uid": "5896401674"})])
        assert "collector" not in load_config(config.root, config.overrides_dir).source("posts").raw


def test_non_weibo_documents_cannot_configure_weibo_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = replace(manifest.platforms()["weibo"], id="other")
    monkeypatch.setattr(sources, "platforms", lambda: {"other": other})
    with pytest.raises(ValueError, match="仅支持微博"):
        sources.document_source_instances([_row(platform="other", collector={"uid": "123"})])
