"""配置加载。

几层，后面的盖住前面的：

1. 默认值，随发布走（一个发布对应一组提交）：

   - 核心自己的默认配置，在包里（``jianshanagent/defaults/*.yaml``）：全局、内核、接入——
     ``app``、``core.*``、``gateway.*``。
   - 插件自己的默认配置，在插件目录里（``<插件目录>/config.yaml``，见 ``core/manifest.py``）：
     功能插件的叫 ``plugin.<id>``，站点插件的叫 ``site.<id>``。插件另外带的配置放在它的
     ``configs/`` 下（对话插件的人设：``configs/persona/persona.default.yaml``，叫
     ``persona/persona.default``）。
   - 用到的站点插件（``core.wiki.yaml`` 的 ``site`` 和它的各级上级）配置里的 ``extend`` 段：
     那个站对通用配置的补充，在这一层就并进去（见 :func:`overlay`）。通用的插件照常读自己的配置，
     不用知道有站点插件这回事。
   - 部署的 ``config/*.yaml``：这个部署和默认值不一样的地方，文件名和要改的那份配置一样，
     只写要改的项（字典逐层合并，别的整个替换）。服务哪个 wiki、用哪个站点插件
     （``core.wiki.yaml``）就是这样写的。部署目录里也可以有包里没有的配置，原样算数。

2. ``config/local/*.yaml``：开发机的调试覆写（不入库，也不进镜像）。
3. 覆写目录（环境变量 ``JIANSHANAGENT_CONFIG_OVERRIDES_DIR``，线上是数据盘上的 ``/data/config``）：
   管理后台里改的配置落在这里，一个配置文件对应一个同名的 YAML，里面只有和前两层不一样的项。
   发布换了版本它还在；删掉某个文件 = 那份配置全部恢复默认。没设这个环境变量就没有这一层。

覆写文件里某一项写成 ``null`` 表示「删掉这一项」（当作默认值里没写它）。
列表是整个替换的，只有插件启用清单（``core.plugins`` 的 ``enabled``）例外：它还要跟上默认清单
后来的变化，不然后台动过一次开关，以后发布新加的插件就再也不会启用（见 :func:`merge_enabled`）。
覆写文件坏了（不是合法的 YAML、不是键值结构）不拦启动：跳过它，原因记在 ``override_errors`` 里。
凭据（``secrets.yaml``）不在这三层里，也不能被覆写。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from jianshanagent.core.manifest import (
    CAPABILITY_EXTEND,
    Manifest,
    feature_manifests,
    installed_sites,
    site_chain,
)

OVERRIDES_ENV = "JIANSHANAGENT_CONFIG_OVERRIDES_DIR"
_SECRET_NAMES = {"secrets", "secrets.example"}
_OVERRIDE_HEADER = (
    "# 由管理后台写入：只有和发布默认值不一样的项（null = 删掉默认值里的这一项）。\n"
    "# 删掉这个文件 = 这份配置全部恢复默认。\n"
)
_PLUGINS_HEADER = (
    "# enabled_defaults_when_saved 是保存时默认的启用清单，不要手改：靠它认出以后的发布新加的插件\n"
    "# （会自动启用）和在后台明确关掉的插件（不会被重新打开）。\n"
)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


#: 不许用覆写（也就是管理后台的网页）改的项，按键名认，不管在哪份配置的哪一层：
#: 接口地址，和「用 secrets.yaml 里的哪个密钥」。密钥是作为请求头发往这些地址的——能在网页上
#: 改它们，登录了后台的人就能让机器人把密钥发到自己的服务器上，而凭据本来不该能从网页上拿到。
#: 要改这些项，改仓库里的配置文件再发布
LOCKED_KEYS = frozenset(
    {
        "base_url",
        "api_url",
        "site_base",
        "auth_header",
        "secret",
        "dsn_env",
        "url_env",
        "token_file",
    }
)
#: 写着「服务哪个 wiki、用哪个站点插件」的那份配置
WIKI_FILE = "core.wiki"
#: 站点插件的配置叫 ``site.<id>``；里面这一段是它对通用配置的补充（配置名 → 要并进去的内容）
SITE_PREFIX = "site."
EXTEND = CAPABILITY_EXTEND

#: 另外按路径锁住的几项（配置名 → 路径）。
#: 关掉管理后台；健康检查的地址和端口（容器的端口映射、健康检查都写死了 8080 和 /healthz）：
#: 在网页上改了会把管理后台自己关在外面，只能上服务器救。
#: 用哪个站点插件：可以写包路径，能在网页上改就等于能让机器人去加载别的代码
LOCKED_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "core.admin": (("enabled",),),
    "app": (("healthcheck",),),
    WIKI_FILE: (("site",),),
    # 从哪个仓库更新、下载走哪个镜像：能在网页上改，就等于能让机器人去下载并运行别人的代码
    "core.update": (("repo",), ("download_prefix",)),
}


def locked_paths(name: str) -> tuple[tuple[str, ...], ...]:
    """``name`` 这份配置里按路径锁住的项。

    站点插件配置里的 ``extend`` 也锁着：它在读配置的第一层就并进了别的配置，在网页上改它不会生效
    ——要改那些项，去改它们所在的那份配置。
    """
    paths = LOCKED_PATHS.get(name, ())
    return (*paths, (EXTEND,)) if name.startswith(SITE_PREFIX) else paths


def is_locked(name: str, path: Sequence[str]) -> bool:
    """``name`` 这份配置里 ``path`` 这一项是不是不许在网页上改（它上面某一层锁着也算）。"""
    return any(key in LOCKED_KEYS for key in path) or any(
        tuple(path[: len(locked)]) == locked for locked in locked_paths(name)
    )


def locked_rules(name: str) -> dict[str, Any]:
    """``name`` 这份配置里哪些项锁着，给页面看的：页面照着把它们显示成只读，不自己另抄一份名单。

    ``keys``：叫这些名字的项，不管在哪一层；``paths``：另外按路径锁住的。
    """
    return {
        "keys": sorted(LOCKED_KEYS),
        "paths": [list(locked) for locked in locked_paths(name)],
    }


def locked_changes(name: str, override: dict[str, Any]) -> list[str]:
    """``name`` 这份配置的覆写里动了哪些不许在网页上改的项。

    返回它们的路径（如 ``wiki.api_url``）；一项都没动返回空列表。
    """
    found: list[str] = []

    def walk(node: Any, here: tuple[str, ...]) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, here)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            path = (*here, str(key))
            if is_locked(name, path):
                found.append(".".join(path))
            else:
                walk(value, path)

    walk(override, ())
    return found


def apply_overrides(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把覆写盖到默认值上：字典逐层合并，列表和单个值整个替换，``null`` 表示删掉这一项。"""
    out = dict(base)
    for key, value in override.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = apply_overrides(out[key], value)
        elif isinstance(value, dict):
            out[key] = apply_overrides({}, value)  # 默认值里没有的一整段：里面的 null 也要去掉
        else:
            out[key] = value
    return out


def overlay(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """把 ``top`` 并到 ``base`` 上：字典逐层合并；列表接在一起（``base`` 的在前，重复的不要）；
    单个值用 ``top`` 的。不动传进来的两个。

    站点插件对通用配置的补充就是这么并进去的：名单（要去掉的 class、给对话模型的工具……）是
    通用的和这个站的合在一起，单个值（这个站的名字、提示词里的说法……）以站点插件给的为准。
    """
    out = dict(base)
    for key, value in top.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = overlay(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            out[key] = current + [item for item in value if item not in current]
        else:
            out[key] = value
    return out


def wiki_site_chain(
    wiki: dict[str, Any], *, workspace_root: Path | None = None
) -> tuple[Manifest, ...]:
    """``core.wiki`` 里写的站点插件（``site``）和它的各级上级，从上到下。"""
    if "sites" in wiki:
        raise ValueError(
            "core.wiki.yaml 里的 sites（站点插件的名单）不用了：改成 site，只写最末一级"
            "（如 site: yys），上级会顺着插件清单自动带出来"
        )
    return site_chain(str(wiki.get("site") or ""), workspace_root)


def site_config_name(site: str) -> str:
    """站点插件的配置叫什么：``yys`` → ``site.yys``；写成包路径的取最后一段。"""
    return f"{SITE_PREFIX}{site.rsplit('.', 1)[-1]}"


def _extend_with_sites(files: dict[str, dict[str, Any]], chain: Sequence[Manifest]) -> None:
    """把各级站点插件对通用配置的补充并进 ``files``（就地改），从上到下。"""
    for manifest in chain:
        source = manifest.config_name
        extend = files.get(source, {}).get(EXTEND) or {}
        where = f"站点插件「{manifest.id}」的配置里的 {EXTEND}"
        if not isinstance(extend, dict):
            raise ValueError(f"{where} 要写成「配置名: 要并进去的内容」")
        if extend and EXTEND not in manifest.capabilities:
            raise ValueError(f"{where}：它的清单（plugin.yaml）的 capabilities 里没有写 {EXTEND}")
        for target, patch in extend.items():
            if target not in files or target.startswith(SITE_PREFIX):
                raise ValueError(f"{where} 里写了「{target}」，没有这份配置")
            if not isinstance(patch, dict):
                raise ValueError(f"{where}.{target} 要写成键值结构")
            if target == WIKI_FILE and "site" in patch:
                raise ValueError(f"{where} 不能改 {WIKI_FILE} 的 site（用哪个站点插件）")
            files[target] = overlay(files[target], patch)


def _same(default: Any, value: Any) -> bool:
    """两个值写进配置算不算一样。8 和 8.0 算一样（网页传回来的数字不分整数小数）；
    1 和 true 在 Python 里相等，但写进配置是两回事。"""
    if isinstance(default, bool) != isinstance(value, bool):
        return False
    return bool(default == value)


def diff_overrides(base: dict[str, Any], edited: dict[str, Any]) -> dict[str, Any]:
    """改完的配置和默认值相比差在哪：:func:`apply_overrides` 的反过程。

    只留不一样的项；默认值里有、改完没有了的项记成 ``None``。
    """
    out: dict[str, Any] = {}
    for key, value in edited.items():
        if key not in base:
            if value is not None:
                out[key] = value
        elif isinstance(base[key], dict) and isinstance(value, dict):
            nested = diff_overrides(base[key], value)
            if nested:
                out[key] = nested
        elif not _same(base[key], value):
            out[key] = value
    for key in base:
        if key not in edited:
            out[key] = None
    return out


#: 插件启用清单在哪份配置里
PLUGINS_FILE = "core.plugins"
#: 那份配置的覆写里和 ``enabled`` 配套的一项：保存的时候，默认的启用清单是什么样。
#: 只在覆写文件里有，读配置时用完就拿掉，生效的配置和页面上都看不到它
ENABLED_BASELINE = "enabled_defaults_when_saved"


def _names(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def merge_enabled(default: list[str], saved: list[str], baseline: list[str] | None) -> list[str]:
    """管理后台存下的插件清单 + 现在的默认清单 → 实际启用哪些、按什么顺序。

    一句话：以存下的清单为准，再跟上默认清单在保存之后的变化——后来加进默认清单的插件自动启用，
    后来从默认清单里删掉的拿掉。``baseline`` 是保存时的默认清单，用来认「后来」：

    - 默认清单里有、``baseline`` 里没有：保存之后的发布新加的。插到默认顺序里排在它前面、
      现在启用着的最近那一个后面（前面的都没启用就放最前）。
    - ``baseline`` 里有、存下的清单里没有：在后台明确关掉的。不会被重新打开。
    - ``baseline`` 里有、默认清单里没有了：后来的发布删掉的。拿掉，不然装插件时找不到它会报错。
    - 存下的清单里有、``baseline`` 和默认清单里都没有：在后台另外启用的（默认不开的），留着。

    ``baseline`` 是 None：旧版本存的覆写，没记这一项，没法知道哪些是后来加的——按存下的清单
    原样用，和以前一样（管理后台启动时会给它补记上，见 :func:`note_plugin_baseline`）。
    """
    if baseline is None:
        return list(saved)
    known, current = set(baseline), set(default)
    merged = [name for name in saved if name in current or name not in known]
    for position, name in enumerate(default):
        if name in known or name in merged:
            continue
        before = (merged.index(item) for item in reversed(default[:position]) if item in merged)
        merged.insert(next(before, -1) + 1, name)
    return merged


def _with_plugins_merged(
    base: dict[str, Any], effective: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    """``core.plugins`` 盖完覆写之后多走的一步：启用清单跟上默认清单的变化，记账的那一项拿掉。"""
    out = {key: value for key, value in effective.items() if key != ENABLED_BASELINE}
    if isinstance(override.get("enabled"), list):
        baseline = override.get(ENABLED_BASELINE)
        out["enabled"] = merge_enabled(
            _names(base.get("enabled")),
            _names(override["enabled"]),
            _names(baseline) if isinstance(baseline, list) else None,
        )
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 mapping: {path}")
    return data


def _config_key(rel: Path) -> str:
    """配置文件的逻辑名：``app`` / ``gateway.qq`` / ``plugin.wiki_query``；人设保留目录
    （``persona/persona.default``）。"""
    if rel.parts and rel.parts[0] == "persona":
        return rel.with_suffix("").as_posix()
    return rel.stem


#: 核心自己的默认配置在包里的位置
DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "defaults"


def resolve_config_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("JIANSHANAGENT_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    # 当前目录下的 config（在实例仓库里运行、容器里的 /app 都是这种）；
    # 再不然是核心仓库自己那个空的 config（在别处运行核心的源码时）
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "config",
        Path("/app/config"),
        here.parents[3] / "config",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    raise FileNotFoundError("找不到 config/ 目录，请设置 JIANSHANAGENT_CONFIG_DIR")


class AppConfig:
    """统一配置视图：按文件名索引 + 常用快捷访问。"""

    def __init__(
        self,
        root: Path,
        files: dict[str, dict[str, Any]],
        secrets: dict[str, Any],
        *,
        base_files: dict[str, dict[str, Any]] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
        overrides_dir: Path | None = None,
        override_errors: dict[str, str] | None = None,
        sources: dict[str, Path] | None = None,
    ):
        self.root = root
        #: 实际生效的配置（默认值 + 覆写）
        self.files = files
        self.secrets = secrets
        #: 默认值：随发布走的配置，加上开发机的 ``config/local``
        self.base_files = base_files if base_files is not None else files
        #: 覆写目录里各个文件的内容（管理后台改过的项）
        self.overrides = overrides or {}
        self.overrides_dir = overrides_dir
        #: 没用上的覆写文件和原因（文件坏了、名字对不上任何配置）
        self.override_errors = override_errors or {}
        #: 各份配置的默认值写在哪个文件里（插件的在插件目录里，其余的在 ``config/``）
        self.sources = sources or {}

    def get(self, name: str) -> dict[str, Any]:
        """按逻辑名取配置，如 ``app`` / ``gateway.qq`` / ``plugin.wiki_query``。"""
        return dict(self.files.get(name, {}))

    def plugin(self, plugin_id: str) -> dict[str, Any]:
        return self.get(f"plugin.{plugin_id}")

    @property
    def site_chain(self) -> tuple[Manifest, ...]:
        """用到的站点插件的清单：``core.wiki`` 的 ``site`` 和它的各级上级，从上到下。"""
        return wiki_site_chain(self.get(WIKI_FILE), workspace_root=self.root.parent)

    @property
    def sites(self) -> list[str]:
        """用到的站点插件的 id，按加载顺序（上级在前）。"""
        return [manifest.id for manifest in self.site_chain]

    def site(self, name: str) -> dict[str, Any]:
        """一个站点插件自己的配置（``site.<id>``）；它没有配置就是空的。"""
        return self.get(site_config_name(name))

    def source_path(self, name: str) -> Path:
        """默认值所在的文件：插件的在插件目录里，其余的是 ``config/<名字>.yaml``。"""
        return self.sources.get(name) or self.root / f"{name}.yaml"

    def override_path(self, name: str) -> Path | None:
        """这份配置的覆写文件；没有覆写目录、或者名字不是已有的配置时是 None。"""
        if self.overrides_dir is None or name not in self.base_files:
            return None
        return self.overrides_dir / f"{name}.yaml"

    @property
    def app(self) -> dict[str, Any]:
        return self.get("app")

    @property
    def qq_credentials(self) -> tuple[str, str]:
        qq = self.secrets.get("qq") or {}
        app_id = str(qq.get("app_id") or os.environ.get("QQ_BOT_APP_ID") or "")
        secret = str(
            qq.get("client_secret")
            or os.environ.get("QQ_BOT_CLIENT_SECRET")
            or os.environ.get("QQ_BOT_APP_SECRET")
            or ""
        )
        return app_id, secret

    @property
    def postgres_dsn(self) -> str:
        storage = self.get("core.storage")
        env_name = storage.get("postgres", {}).get("dsn_env", "JIANSHANAGENT_POSTGRES_DSN")
        return os.environ.get(env_name) or (self.secrets.get("postgres") or {}).get("dsn") or ""

    @property
    def redis_url(self) -> str:
        storage = self.get("core.storage")
        env_name = storage.get("redis", {}).get("url_env", "JIANSHANAGENT_REDIS_URL")
        return (
            os.environ.get(env_name)
            or (self.secrets.get("redis") or {}).get("url")
            or "redis://127.0.0.1:6379/0"
        )


def resolve_overrides_dir(
    explicit: str | Path | None = None, *, config_root: Path | None = None
) -> Path | None:
    if explicit is not None:
        return Path(explicit).expanduser().resolve() if explicit else None
    if OVERRIDES_ENV in os.environ:
        env = os.environ[OVERRIDES_ENV].strip()
        return Path(env).expanduser().resolve() if env else None
    if config_root is not None:
        return (config_root.parent / "data" / "config").resolve()
    return None


def load_config(
    config_dir: str | Path | None = None,
    overrides_dir: str | Path | None = None,
    *,
    with_overrides: bool = True,
) -> AppConfig:
    """读配置。``with_overrides=False`` 时不看覆写目录（覆写导致起不来时退回默认值用）。"""
    root = resolve_config_dir(config_dir)

    # 部署目录里的配置
    deployed: dict[str, dict[str, Any]] = {}
    sources: dict[str, Path] = {}
    for path in sorted(root.rglob("*.yaml")):
        if path.name.startswith("."):
            continue
        rel = path.relative_to(root)
        # secrets / local 单独处理
        if rel.as_posix() == "secrets.yaml" or rel.as_posix() == "secrets.example.yaml":
            continue
        if rel.parts and rel.parts[0] == "local":
            continue
        deployed[_config_key(rel)] = _load_yaml(path)
        sources[_config_key(rel)] = path

    local: dict[str, dict[str, Any]] = {}
    local_dir = root / "local"
    if local_dir.is_dir():
        for path in sorted(local_dir.rglob("*.yaml")):
            local[path.stem] = _load_yaml(path)

    # 核心自己的默认配置（app、core.*、gateway.*），随包走
    packaged: dict[str, dict[str, Any]] = {}
    for path in sorted(DEFAULTS_DIR.glob("*.yaml")):
        packaged[path.stem] = _load_yaml(path)
        sources[path.stem] = path

    # 用哪个站点插件（开发机可以在 local 里换），连同它的各级上级
    workspace_root = root.parent
    wiki = _deep_merge(packaged.get(WIKI_FILE, {}), deployed.get(WIKI_FILE, {}))
    chain = wiki_site_chain(
        _deep_merge(wiki, local.get(WIKI_FILE, {})), workspace_root=workspace_root
    )

    # 插件自己的默认配置：能用的功能插件和装着的站点插件都读（没用到的站点插件也读，后台里看得见），
    # 没登记、按包路径写的站点插件只读用到的
    for manifest in (
        *feature_manifests(workspace_root).values(),
        *installed_sites(workspace_root).values(),
        *chain,
    ):
        if manifest.config_path.is_file():
            packaged[manifest.config_name] = _load_yaml(manifest.config_path)
            sources[manifest.config_name] = manifest.config_path
        # 插件另外带的配置（对话插件的人设），名字的取法和 config/ 里的一样
        for path in sorted(manifest.extra_configs.rglob("*.yaml")):
            key = _config_key(path.relative_to(manifest.extra_configs))
            packaged[key] = _load_yaml(path)
            sources[key] = path

    base = {**deployed, **packaged}
    # 站点插件对通用配置的补充
    _extend_with_sites(base, chain)
    # 部署目录里和默认配置同名的文件：这次部署对它的改动（只写要改的项）
    for key in packaged.keys() & deployed.keys():
        base[key] = _deep_merge(base[key], deployed[key])
    # local 同名覆写
    for key, values in local.items():
        base[key] = _deep_merge(base.get(key, {}), values)

    files = dict(base)
    overrides: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    chosen = resolve_overrides_dir(overrides_dir, config_root=root)
    if chosen is not None and with_overrides and chosen.is_dir():
        for path in sorted(chosen.rglob("*.yaml")):
            rel = path.relative_to(chosen)
            if any(part.startswith(".") for part in rel.parts):
                continue
            key = _config_key(rel)
            if key in _SECRET_NAMES:
                errors[key] = "凭据不能放在覆写目录里"
                continue
            if key not in base:
                errors[key] = "没有叫这个名字的配置文件"
                continue
            try:
                override = _load_yaml(path)
            except (yaml.YAMLError, ValueError, OSError) as exc:
                errors[key] = f"读不了：{exc}"[:300]
                continue
            if override:
                files[key] = apply_overrides(base[key], override)
                if key == PLUGINS_FILE:
                    # 在这里合并，装插件、重新装配、管理后台读到的就都是合并好的清单
                    files[key] = _with_plugins_merged(base[key], files[key], override)
                    override = {k: v for k, v in override.items() if k != ENABLED_BASELINE}
                if override:
                    overrides[key] = override

    secrets = _load_yaml(root / "secrets.yaml")
    return AppConfig(
        root=root,
        files=files,
        secrets=secrets,
        base_files=base,
        overrides=overrides,
        overrides_dir=chosen,
        override_errors=errors,
        sources=sources,
    )


def write_override(config: AppConfig, name: str, edited: dict[str, Any]) -> dict[str, Any]:
    """把改完的一份配置存成覆写文件，返回写进去的内容（和默认值不一样的项）。

    和默认值完全一样时删掉覆写文件。只动磁盘上的文件，``config`` 本身不变——要生效得重新
    :func:`load_config`。先写到临时文件再改名，写到一半断电不会留下半个文件。
    """
    path = config.override_path(name)
    if path is None:
        if config.overrides_dir is None:
            raise ValueError(f"没有配置覆写目录（环境变量 {OVERRIDES_ENV}），改动没有地方存")
        raise ValueError(f"没有叫「{name}」的配置文件")
    base = config.base_files[name]
    if name == PLUGINS_FILE:
        edited = {key: value for key, value in edited.items() if key != ENABLED_BASELINE}
    override = diff_overrides(base, edited)
    if not override:
        path.unlink(missing_ok=True)
        return {}
    if name == PLUGINS_FILE and isinstance(override.get("enabled"), list):
        # 存下启用清单的同时记下现在的默认清单：以后默认清单变了，才认得出哪些是新加的
        override[ENABLED_BASELINE] = _names(base.get("enabled"))
    _write_override_file(path, name, override)
    return override


def _write_override_file(path: Path, name: str, override: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = _OVERRIDE_HEADER + (_PLUGINS_HEADER if name == PLUGINS_FILE else "")
    text = header + yaml.safe_dump(override, allow_unicode=True, sort_keys=False)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def note_plugin_baseline(config: AppConfig) -> bool:
    """旧版本存的插件清单覆写没记「保存时的默认清单」：按现在的默认清单给它补记上。返回补了没有。

    旧的覆写里只有一整份启用清单，分不出哪些插件是明确关掉的、哪些是后来的发布新加的。
    补记以后，现在没启用的都算「明确关掉的」（实际启用哪些和补之前一模一样），
    从这以后发布新加的插件才认得出来、会自动启用。
    """
    path = config.override_path(PLUGINS_FILE)
    if path is None or not path.is_file():
        return False
    override = _load_yaml(path)
    if not isinstance(override.get("enabled"), list) or ENABLED_BASELINE in override:
        return False
    override[ENABLED_BASELINE] = _names(config.base_files[PLUGINS_FILE].get("enabled"))
    _write_override_file(path, PLUGINS_FILE, override)
    return True


def restore_override(config: AppConfig, name: str, previous: str | None) -> None:
    """把覆写文件恢复成之前的原文（``None`` = 之前没有这个文件）。改动没能生效时撤回用。"""
    path = config.override_path(name)
    if path is None:
        return
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(previous, encoding="utf-8")
