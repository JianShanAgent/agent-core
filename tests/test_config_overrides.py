"""配置的覆写层：管理后台改的配置怎么盖到默认值上、怎么存、坏了怎么办。"""

from pathlib import Path

import pytest
import yaml

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import (
    ENABLED_BASELINE,
    apply_overrides,
    diff_overrides,
    is_locked,
    load_config,
    locked_changes,
    locked_rules,
    merge_enabled,
    note_plugin_baseline,
    restore_override,
    write_override,
)

BASE = {
    "enabled": True,
    "timeout": 8,
    "ratio": 0.2,
    "names": ["a", "b"],
    "profiles": {
        "chat": {"model": "pro", "thinking": True},
        "quick": {"model": "pro", "thinking": False, "temperature": 0.2},
    },
}


def _config_dir(tmp_path: Path) -> tuple[Path, Path]:
    root, overrides = tmp_path / "config", tmp_path / "overrides"
    (root / "persona").mkdir(parents=True)
    (root / "local").mkdir()
    (root / "plugin.demo.yaml").write_text(
        "enabled: true\ntimeout: 8\nnested:\n  a: 1\n  b: 2\nitems: [x, y]\n", "utf-8"
    )
    (root / "persona" / "persona.custom.yaml").write_text("name: 助手\n", "utf-8")
    (root / "local" / "plugin.demo.yaml").write_text("timeout: 9\n", "utf-8")
    (root / "secrets.yaml").write_text("llm:\n  api_key: k\n", "utf-8")
    return root, overrides


def test_apply_and_diff_are_inverse() -> None:
    edited = {
        "enabled": False,  # 改了一个值
        "timeout": 8.0,  # 网页传回来的数字不分整数小数：不算改动
        "ratio": 0.2,
        "names": ["a", "c"],  # 列表整个替换
        "profiles": {
            "chat": {"model": "pro", "thinking": False},  # 里面改一项
            "extract": {"model": "flash", "thinking": False},  # 加一档；quick 这一档删掉了
        },
    }
    override = diff_overrides(BASE, edited)
    assert override == {
        "enabled": False,
        "names": ["a", "c"],
        "profiles": {
            "chat": {"thinking": False},
            "extract": {"model": "flash", "thinking": False},
            "quick": None,  # 默认值里有、改完没有了：记成 null
        },
    }
    merged = apply_overrides(BASE, override)
    assert merged == {**edited, "timeout": 8}
    assert "quick" not in merged["profiles"]
    # 没改就没有覆写
    assert diff_overrides(BASE, apply_overrides(BASE, {})) == {}
    # 1 和 true 在 Python 里相等，写进配置是两回事
    assert diff_overrides({"n": 1}, {"n": True}) == {"n": True}
    # 默认值不会被改到
    assert BASE["profiles"]["chat"]["thinking"] is True


def test_load_layers_defaults_local_and_overrides(tmp_path: Path) -> None:
    root, overrides = _config_dir(tmp_path)
    overrides.mkdir()
    (overrides / "plugin.demo.yaml").write_text(
        "nested:\n  a: 10\n  b: null\nitems: [z]\n", "utf-8"
    )
    (overrides / "persona").mkdir()
    (overrides / "persona" / "persona.custom.yaml").write_text("name: 新名字\n", "utf-8")

    config = load_config(root, overrides)
    # 默认值 ← 本机覆写 ← 管理后台的覆写；null 是删掉那一项
    assert config.plugin("demo") == {
        "enabled": True,
        "timeout": 9,
        "nested": {"a": 10},
        "items": ["z"],
    }
    assert config.base_files["plugin.demo"]["nested"] == {"a": 1, "b": 2}
    assert config.get("persona/persona.custom") == {"name": "新名字"}
    assert sorted(config.overrides) == ["persona/persona.custom", "plugin.demo"]
    assert config.override_errors == {}
    # 覆写导致起不来时用得上：整层不看
    plain = load_config(root, overrides, with_overrides=False)
    assert plain.plugin("demo")["nested"] == {"a": 1, "b": 2} and plain.overrides == {}
    # 没设环境变量时默认落在实例目录下的 data/config；显式设为空字符串时禁用覆写目录
    assert load_config(root).overrides_dir == (root.parent / "data" / "config").resolve()
    assert load_config(root, "").overrides_dir is None


def test_broken_or_stray_override_files_are_skipped_not_fatal(tmp_path: Path) -> None:
    root, overrides = _config_dir(tmp_path)
    overrides.mkdir()
    (overrides / "plugin.demo.yaml").write_text("timeout: [1, 2\n", "utf-8")  # 不是合法的 YAML
    (overrides / "plugin.gone.yaml").write_text("x: 1\n", "utf-8")  # 没有这份配置
    (overrides / "secrets.yaml").write_text("llm:\n  api_key: evil\n", "utf-8")  # 凭据不能覆写

    config = load_config(root, overrides)
    assert config.plugin("demo")["timeout"] == 9  # 坏文件被跳过，用默认值
    assert set(config.override_errors) == {"plugin.demo", "plugin.gone", "secrets"}
    assert config.secrets == {"llm": {"api_key": "k"}}


def test_write_override_stores_only_the_difference(tmp_path: Path) -> None:
    root, overrides = _config_dir(tmp_path)
    config = load_config(root, overrides)
    edited = {**config.plugin("demo"), "timeout": 30, "nested": {"a": 1}}
    assert write_override(config, "plugin.demo", edited) == {"timeout": 30, "nested": {"b": None}}
    path = overrides / "plugin.demo.yaml"
    assert "timeout: 30" in path.read_text("utf-8") and "enabled" not in path.read_text("utf-8")
    assert load_config(root, overrides).plugin("demo") == edited

    # 撤回：恢复成之前的原文（之前没有文件就删掉）
    restore_override(config, "plugin.demo", "timeout: 12\n")
    assert load_config(root, overrides).plugin("demo")["timeout"] == 12
    restore_override(config, "plugin.demo", None)
    assert not path.exists()

    # 改回和默认值一样：覆写文件删掉
    write_override(config, "plugin.demo", edited)
    assert write_override(config, "plugin.demo", config.base_files["plugin.demo"]) == {}
    assert not path.exists()

    with pytest.raises(ValueError, match="没有叫"):
        write_override(config, "plugin.nope", {})
    with pytest.raises(ValueError, match="没有配置覆写目录"):
        write_override(load_config(root, ""), "plugin.demo", {})


def _plugins_dir(tmp_path: Path, default: list[str]) -> tuple[Path, Path]:
    root, overrides = _config_dir(tmp_path)
    _release(root, default)
    return root, overrides


def _release(root: Path, default: list[str]) -> None:
    """发布一版：默认的插件启用清单换成 ``default``。"""
    (root / "core.plugins.yaml").write_text(yaml.safe_dump({"enabled": default}), "utf-8")


def test_merging_the_saved_plugin_list_with_a_newer_default_list() -> None:
    saved, baseline = ["safety", "chat", "wiki"], ["safety", "wiki", "memory", "chat"]
    # 默认清单没变：就是存下的清单（顺序是后台调过的，memory 是后台关掉的）
    assert merge_enabled(baseline, saved, baseline) == saved
    # 后来的发布加了三个：各自插在默认顺序里排在它前面、现在启用着的最近那一个后面
    newer = ["guard", "safety", "wiki", "kb", "memory", "chat", "log"]
    assert merge_enabled(newer, saved, baseline) == ["guard", "safety", "chat", "log", "wiki", "kb"]
    # 连着新加的几个保持默认清单里的先后；前面那个被关掉了，就再往前找
    assert merge_enabled(["safety", "memory", "a", "b"], ["safety"], ["safety", "memory"]) == [
        "safety",
        "a",
        "b",
    ]
    # 后来的发布删掉的：拿掉。后台另外启用的（默认清单里从来没有）：留着
    assert merge_enabled(["safety", "chat"], [*saved, "extra"], baseline) == [
        "safety",
        "chat",
        "extra",
    ]
    # 关掉的不会因为默认清单变了被重新打开；存下的清单里已经有的不会再插一遍
    assert "memory" not in merge_enabled(newer, saved, baseline)
    assert merge_enabled(["a", "b"], ["b", "a"], ["a"]) == ["b", "a"]
    # 旧版本存的覆写没记保存时的默认清单：原样用
    assert merge_enabled(newer, ["safety", "gone"], None) == ["safety", "gone"]


def test_default_plugins_added_by_a_later_release_are_enabled_automatically(
    tmp_path: Path,
) -> None:
    root, overrides = _plugins_dir(tmp_path, ["safety", "wiki", "memory", "chat"])
    config = load_config(root, overrides)
    # 后台关掉 memory、把 chat 挪到 wiki 前面
    stored = write_override(config, "core.plugins", {"enabled": ["safety", "chat", "wiki"]})
    assert stored == {
        "enabled": ["safety", "chat", "wiki"],
        ENABLED_BASELINE: ["safety", "wiki", "memory", "chat"],  # 保存时的默认清单
    }
    assert "不要手改" in (overrides / "core.plugins.yaml").read_text("utf-8")

    config = load_config(root, overrides)
    assert config.get("core.plugins") == {"enabled": ["safety", "chat", "wiki"]}
    # 记账的那一项只在覆写文件里：生效的配置、「改了哪几项」里都没有它
    assert config.overrides["core.plugins"] == {"enabled": ["safety", "chat", "wiki"]}

    # 发布新版本：默认清单多了 kb（排在 wiki 后面）和 log（排在 chat 后面），少了 memory
    _release(root, ["safety", "wiki", "kb", "chat", "log"])
    config = load_config(root, overrides)
    # kb、log 自动启用；memory 是后台关的，又被发布删掉了，怎么都不该出现
    assert config.get("core.plugins")["enabled"] == ["safety", "chat", "log", "wiki", "kb"]

    # 再发布一版把 wiki 从默认清单里删掉：覆写里还列着它，也不会再去装它
    # （kb 原来跟在 wiki 后面，wiki 没了就跟在默认顺序里再往前的 safety 后面）
    _release(root, ["safety", "kb", "chat", "log"])
    assert load_config(root, overrides).get("core.plugins")["enabled"] == [
        "safety",
        "kb",
        "chat",
        "log",
    ]
    # 不看覆写（覆写导致起不来时的退路）就是默认清单
    assert load_config(root, overrides, with_overrides=False).get("core.plugins")["enabled"] == [
        "safety",
        "kb",
        "chat",
        "log",
    ]


def test_a_plugin_switched_off_in_the_admin_stays_off(tmp_path: Path) -> None:
    root, overrides = _plugins_dir(tmp_path, ["safety", "memory", "chat"])
    write_override(load_config(root, overrides), "core.plugins", {"enabled": ["safety", "chat"]})
    _release(root, ["safety", "memory", "chat", "log"])
    config = load_config(root, overrides)
    assert config.get("core.plugins")["enabled"] == ["safety", "chat", "log"]

    # 在新的默认清单下再存一次（把 log 也关掉）：两个都还是关着的
    write_override(config, "core.plugins", {"enabled": ["safety", "chat"]})
    stored = yaml.safe_load((overrides / "core.plugins.yaml").read_text("utf-8"))
    assert stored[ENABLED_BASELINE] == ["safety", "memory", "chat", "log"]
    assert load_config(root, overrides).get("core.plugins")["enabled"] == ["safety", "chat"]
    # 页面把整份配置传回来保存时带着记账的那一项也不要紧：按现在的默认清单重新记
    write_override(config, "core.plugins", {"enabled": ["safety"], ENABLED_BASELINE: ["x"]})
    stored = yaml.safe_load((overrides / "core.plugins.yaml").read_text("utf-8"))
    assert stored == {"enabled": ["safety"], ENABLED_BASELINE: ["safety", "memory", "chat", "log"]}

    # 改回和默认一样：覆写文件删掉，从此完全跟着默认清单走
    write_override(config, "core.plugins", {"enabled": ["safety", "memory", "chat", "log"]})
    assert not (overrides / "core.plugins.yaml").exists()


def test_plugin_overrides_saved_by_older_versions_still_work(tmp_path: Path) -> None:
    root, overrides = _plugins_dir(tmp_path, ["safety", "memory", "chat", "log"])
    overrides.mkdir()
    path = overrides / "core.plugins.yaml"
    # 旧版本的后台存的：只有一整份启用清单
    path.write_text("enabled:\n- safety\n- chat\n", "utf-8")
    config = load_config(root, overrides)
    assert config.get("core.plugins")["enabled"] == ["safety", "chat"]  # 和旧版本读出来的一样

    # 管理后台启动时补记一次：从这时起算，没启用的（memory、log）都当成明确关掉的
    assert note_plugin_baseline(config) is True
    stored = yaml.safe_load(path.read_text("utf-8"))
    assert stored == {
        "enabled": ["safety", "chat"],
        ENABLED_BASELINE: ["safety", "memory", "chat", "log"],
    }
    assert note_plugin_baseline(load_config(root, overrides)) is False  # 补过了就不再动
    assert load_config(root, overrides).get("core.plugins")["enabled"] == ["safety", "chat"]

    # 之后的发布新加的就认得出来了
    _release(root, ["safety", "memory", "kb", "chat", "log"])
    assert load_config(root, overrides).get("core.plugins")["enabled"] == ["safety", "kb", "chat"]

    # 没有覆写文件、覆写里没改启用清单、没有覆写目录：都没什么可补的
    path.unlink()
    assert note_plugin_baseline(load_config(root, overrides)) is False
    path.write_text("other: 1\n", "utf-8")
    assert note_plugin_baseline(load_config(root, overrides)) is False
    assert note_plugin_baseline(load_config(root)) is False
    # 记账的那一项被手改坏了（不是列表）：当成没记，按存下的清单原样用，不报错
    path.write_text(f"enabled: [safety, gone]\n{ENABLED_BASELINE}: oops\n", "utf-8")
    assert load_config(root, overrides).get("core.plugins") == {"enabled": ["safety", "gone"]}


def test_which_items_are_locked_is_one_rule_shared_by_saving_and_the_page() -> None:
    assert is_locked("core.llm", ("base_url",))
    assert is_locked("core.embedding", ("providers", "gitee", "secret"))
    assert is_locked("core.embedding", ("providers", "gitee", "secret", "deeper"))  # 上面锁着也算
    assert not is_locked("core.llm", ("timeout_seconds",))
    # 按路径锁的只在那一份配置里锁
    assert is_locked("core.admin", ("enabled",)) and not is_locked("core.llm", ("enabled",))
    assert is_locked("app", ("healthcheck", "port")) and not is_locked("app", ("log", "level"))

    rules = locked_rules("app")
    assert rules["paths"] == [["healthcheck"]] and "base_url" in rules["keys"]
    assert locked_rules("core.llm")["paths"] == []
    # 保存时的检查用的是同一个判断
    assert locked_changes("app", {"healthcheck": {"port": 1}, "log": {"level": "x"}}) == [
        "healthcheck"
    ]
    assert locked_changes("core.embedding", {"providers": {"mine": {"secret": "llm"}}}) == [
        "providers.mine.secret"
    ]


def test_repo_config_loads_the_same_without_an_overrides_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = FULL_CONFIG_DIR
    monkeypatch.delenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", raising=False)
    plain = load_config(repo)
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", str(tmp_path / "empty"))
    layered = load_config(repo)  # 覆写目录还不存在：和没有一样
    assert layered.files == plain.files and layered.overrides == {}
    assert layered.overrides_dir == (tmp_path / "empty").resolve()
