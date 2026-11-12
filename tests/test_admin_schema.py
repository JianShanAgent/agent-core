"""管理后台的配置表单：从 YAML 的注释里取每一项的说明，从默认值看每一项的类型。"""

from pathlib import Path
from typing import Any

import yaml

from deployment import FULL_CONFIG_DIR
from jianshanagent.admin.schema import build_schema, count_leaves, read_comments
from jianshanagent.core.config import load_config, locked_changes

CONFIG = FULL_CONFIG_DIR
#: 仓库里的每份配置：配置名 → 默认值写在哪个文件里（部署级的在 config/，插件的在各自的目录里）
SOURCES: dict[str, Path] = dict(sorted(load_config(CONFIG).sources.items()))

SAMPLE = """\
# 文件说明第一行
# 文件说明第二行
enabled: true

# 上面隔了空行：这是 base_url 的说明
#   缩进的续行也算
base_url: "https://x.test/v1#frag"   # 引号里的 # 不是注释
profiles:                          # 各档模型
  chat:                           # 面向用户
    model: pro                    # 额度是 flash 的 3 倍
    # 必须开思考。
    # 关着会编。
    thinking: true
progress:
  analyze: "正在查…"    # 第一句
                       # 续行：和上一行的注释对齐
  site: ""
prompt: |
  这里面的 fake_key: 不是配置项
  # 也不是注释
items:
  - name: a            # 列表里面的不单独给说明
    deep: 1
after_list: 3          # 列表后面的照常
"""


def _leaf_paths(node: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """YAML 里所有不在列表里的键（从外到里）。"""
    found: set[tuple[str, ...]] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add((*prefix, str(key)))
            found |= _leaf_paths(value, (*prefix, str(key)))
    return found


def test_comments_are_attached_by_position() -> None:
    header, notes = read_comments(SAMPLE)
    assert header == "文件说明第一行\n文件说明第二行"
    assert ("enabled",) not in notes  # 最上面那段归整个文件，不归第一项
    assert notes[("base_url",)] == (
        "上面隔了空行：这是 base_url 的说明\n缩进的续行也算\n引号里的 # 不是注释"
    )
    assert notes[("profiles",)] == "各档模型"
    assert notes[("profiles", "chat", "model")] == "额度是 flash 的 3 倍"
    assert notes[("profiles", "chat", "thinking")] == "必须开思考。\n关着会编。"
    assert notes[("progress", "analyze")] == "第一句\n续行：和上一行的注释对齐"
    assert ("progress", "site") not in notes
    assert notes[("after_list",)] == "列表后面的照常"
    # 多行文字的内容、列表里面的键，都不会被当成配置项
    assert not any("fake_key" in path or "name" in path or "deep" in path for path in notes)


def test_every_repo_config_parses_without_phantom_keys() -> None:
    """仓库里的每份配置：注释对上的键都真的存在（没有把正文、列表项认成键）。"""
    assert len(SOURCES) >= 15 and SOURCES["plugin.chat"].name == "config.yaml"
    described = 0
    for name, path in SOURCES.items():
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        header, notes = read_comments(text)
        phantom = set(notes) - _leaf_paths(data)
        assert not phantom, f"{name}: {sorted(phantom)}"
        assert header, name
        described += len(notes)
    assert described > 150  # 配置注释写得细：大部分项都有说明


def test_repo_descriptions_land_on_the_right_fields() -> None:
    llm_header, llm = read_comments((CONFIG / "core.llm.yaml").read_text("utf-8"))
    assert llm_header.startswith("LLM：小米 MiMo")
    assert "必须开思考" in llm[("profiles", "chat", "thinking")]
    assert "额度消耗是 flash 的 3 倍" in llm[("profiles", "chat", "model")]
    assert "0 = 不按时间过期" in llm[("history", "idle_expire_seconds")]

    _, research = read_comments(SOURCES["plugin.research"].read_text("utf-8"))
    analyze = research[("progress", "analyze")]
    assert "第一句" in analyze and "要把这句挪到 kb 上" in analyze  # 行尾注释 + 对齐的续行
    assert "查证员" in research[("profiles", "analyst")]
    assert "删掉这一项" in research[("profiles", "quick")]  # 紧挨在上面的两行注释


def test_schema_kinds_follow_the_default_values() -> None:
    source = """\
enabled: true
think_timeout_seconds: 75        # 最多等多久
tools: [wiki_filter, memory_save]
stages: [[kb, site], web]        # 列表里还套着列表：表单里按 JSON 整个改
profiles:                        # 各个角色用哪一档
  analyst: quick                 # 查证员
labels:
  kb: 知识库
"""
    base = yaml.safe_load(source)
    _, notes = read_comments(source)
    effective = {**base, "labels": {**base["labels"], "extra": "加出来的"}}
    fields = {field["key"]: field for field in build_schema(base, effective, notes)}

    assert fields["enabled"]["kind"] == "bool"
    assert fields["think_timeout_seconds"]["kind"] == "int"
    assert fields["think_timeout_seconds"]["description"] == "最多等多久"
    assert fields["tools"]["kind"] == "list" and fields["tools"]["items"] == "str"
    assert fields["stages"]["kind"] == "json"
    assert fields["profiles"]["kind"] == "group" and fields["profiles"]["description"]
    analyst = {f["key"]: f for f in fields["profiles"]["children"]}["analyst"]
    assert analyst["kind"] == "str" and analyst["path"] == ["profiles", "analyst"]
    assert analyst["description"] == "查证员"
    labels = {f["key"]: f for f in fields["labels"]["children"]}
    assert labels["extra"]["added"] is True and labels["kb"]["added"] is False

    # 仓库里真的配置也都生成得出表单（每一份、每一项都有类型）
    for path in SOURCES.values():
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        built = build_schema(data, data, read_comments(text)[1])
        assert [field["key"] for field in built] == [str(key) for key in data], path.name

    mixed = build_schema(
        {"ratio": 0.2, "text": "一行", "long": "很长" * 50, "posts": [{"a": 1}], "none": None},
        {},
        {},
    )
    assert [field["kind"] for field in mixed] == ["float", "str", "text", "json", "json"]
    assert count_leaves({"a": 1, "b": {"c": 2, "d": {"e": None}}, "f": [1, 2]}) == 4


def _flat(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for field in fields:
        found[".".join(field["path"])] = field
        found.update(_flat(field.get("children") or []))
    return found


def test_items_that_cannot_be_changed_from_the_web_are_marked_locked() -> None:
    base = {
        "enabled": True,
        "base_url": "https://x.test/v1",
        "timeout_seconds": 60,
        "providers": {"gitee": {"base_url": "https://g.test", "secret": "embedding", "model": "m"}},
        "healthcheck": {"port": 8080},
        "mirrors": [{"api_url": "https://m.test/api.php"}],  # 列表里套着锁住的项
        "posts": [{"title": "x"}],
        "names": ["a"],
    }
    effective = {
        **base,
        # 覆写里加出来的一档（手改覆写文件才会有）：里面的地址、密钥名一样锁着
        "providers": {**base["providers"], "mine": {"base_url": "https://e.test", "model": "n"}},
        "extra": [{"secret": "llm"}],
    }
    fields = _flat(build_schema(base, effective, {}, name="core.embedding"))
    locked = {path for path, field in fields.items() if field["locked"]}
    assert locked == {
        "base_url",
        "providers.gitee.base_url",
        "providers.gitee.secret",
        "providers.mine.base_url",
        "mirrors",  # 列表只能整个换，换上去的还带着锁住的项就存不上：整个标成只读
        "extra",
    }
    assert all(isinstance(field["locked"], bool) for field in fields.values())  # 每一项都带
    # 页面上标的和保存时拒绝的是同一批：把标成只读的每一项改掉，保存时的检查都拦得住
    for path in locked:
        now = effective
        for key in path.split("."):
            now = now[key]
        # 列表：改的是里面别的项，锁住的那一项原样带着
        nested: Any = [{**now[0], "note": "改了"}] if isinstance(now, list) else "改了"
        for key in reversed(path.split(".")):
            nested = {key: nested}
        assert locked_changes("core.embedding", nested), path

    # 有两处是按「哪份配置的哪一项」锁的：换一份配置就不锁
    assert _flat(build_schema(base, base, {}, name="core.admin"))["enabled"]["locked"] is True
    app = _flat(build_schema(base, base, {}, name="app"))
    assert app["healthcheck"]["locked"] is True and app["enabled"]["locked"] is False
    assert app["healthcheck.port"]["locked"] is True  # 一组锁着，里面的每一项也锁着
    assert not _flat(build_schema(base, base, {}, name="core.embedding"))["healthcheck"]["locked"]

    # 仓库里的配置：现在锁着的就是这些（名单变了这里要跟着看一眼页面）
    found: dict[str, set[str]] = {}
    for name, file in SOURCES.items():
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        marked = _flat(build_schema(data, data, {}, name=name))
        paths = {path for path, field in marked.items() if field["locked"]}
        # 一组整个锁着的只记那一组
        paths = {p for p in paths if not any(p.startswith(f"{other}.") for other in paths)}
        if paths:
            found[name] = paths
    assert found == {
        "app": {"healthcheck"},
        # 平台对通用配置的补充：读配置时就并进去了，在网页上改它不会生效
        "platform.bwiki": {"extend"},
        "platform.huiji": {"extend"},
        "core.admin": {"enabled", "token_file"},
        # 站点目录在哪：决定有哪些站点插件能下载、从哪下载、wiki 的地址是什么
        "core.catalog": {"repo", "path", "ref", "api_url", "download_prefix"},
        "core.embedding": {
            "providers.gitee.base_url",
            "providers.gitee.secret",
            "providers.local.base_url",
            "providers.local.secret",
        },
        "core.llm": {"base_url"},
        "core.storage": {"postgres.dsn_env", "redis.url_env"},
        # 从哪个仓库更新、下载走哪个镜像：决定机器人下载并运行谁的代码
        "core.update": {"repo", "api_url", "download_prefix"},
        "core.websearch": {
            "providers.gitee_ai.base_url",
            "providers.gitee_ai.secret",
            "providers.mimo.base_url",
            "providers.mimo.secret",
        },
        "core.wiki": {"wikis"},  # 挂哪几个 wiki：里面有地址和用哪个站点插件
    }
