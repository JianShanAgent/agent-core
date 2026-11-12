"""管理后台的接口：登录把关、改配置（生效、撤回、要重启的）、插件启停、网页对话、日志。

用真的 ``Runtime`` 装一套（不连 QQ、不监听端口、没有数据库），配置是仓库配置的一份拷贝。
"""

import hashlib
import hmac
import json
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from deployment import FULL_CONFIG_DIR
from jianshanagent.admin import auth as auth_module
from jianshanagent.admin.auth import COOKIE, SESSIONS_FILE, Auth, load_token, sessions_file
from jianshanagent.admin.core import Admin, AdminSettings
from jianshanagent.admin.queries import CHAT_DAYS, SESSION_DAYS
from jianshanagent.core import trace
from jianshanagent.core.config import ENABLED_BASELINE, AppConfig, load_config
from jianshanagent.core.logging import LOG_BUFFER
from jianshanagent.core.messages import InboundMessage, OutboundMessage
from jianshanagent.core.plugin import CommandSpec
from jianshanagent.runtime import Runtime

REPO_CONFIG = FULL_CONFIG_DIR
TOKEN = "test-token-0123456789"
WRITE = {"X-Requested-With": "jianshanagent-admin"}


def _copy_config(tmp_path: Path) -> Path:
    """仓库配置的一份拷贝。令牌文件挪到临时目录里：退出登录要在它旁边记东西，不能去碰工作目录。"""
    root = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, root, ignore=shutil.ignore_patterns("local", "secrets.yaml"))
    admin = root / "core.admin.yaml"
    source = admin.read_text("utf-8")
    assert "token_file: data/admin/token" in source
    admin.write_text(
        source.replace(
            "token_file: data/admin/token", f"token_file: {tmp_path / 'admin' / 'token'}"
        ),
        encoding="utf-8",
    )
    return root


class Env:
    def __init__(self, runtime: Runtime, client: TestClient, overrides: Path, root: Path):
        self.runtime = runtime
        self.client = client
        self.overrides = overrides
        #: 配置目录（仓库配置的拷贝）：改它里面的文件 = 模拟发布了新的默认值
        self.root = root

    async def login(self) -> None:
        response = await self.client.post("/api/login", json={"token": TOKEN})
        assert response.status == 200, await response.text()

    async def get(self, path: str, status: int = 200) -> Any:
        response = await self.client.get(path)
        assert response.status == status, await response.text()
        return await response.json()

    async def send(self, method: str, path: str, body: Any = None, status: int = 200) -> Any:
        response = await self.client.request(method, path, json=body, headers=WRITE)
        assert response.status == status, await response.text()
        return await response.json()

    async def chat(self, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """发一条网页对话，返回收到的事件。"""
        response = await self.client.post("/api/chat", json=body, headers=WRITE)
        assert response.status == 200, await response.text()
        events: list[tuple[str, dict[str, Any]]] = []
        for block in (await response.text()).strip().split("\n\n"):
            name, data = block.split("\n", 1)
            events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
        return events


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    root, overrides = _copy_config(tmp_path), tmp_path / "overrides"
    (root / "secrets.yaml").write_text(
        f"llm:\n  api_key: tp-test\nadmin:\n  token: {TOKEN}\n", encoding="utf-8"
    )
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(root))
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", str(overrides))
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("JIANSHANAGENT_LLM_API_KEY", raising=False)
    # 测试里不动全局的日志配置（它会缓存住被 pytest 接管的标准输出）
    monkeypatch.setattr("jianshanagent.runtime.setup_logging", lambda **_kwargs: None)
    runtime = Runtime(dry_run=True)
    await runtime.prepare()
    client = TestClient(TestServer(runtime.build_app()))
    await client.start_server()
    try:
        yield Env(runtime, client, overrides, root)
    finally:
        await client.close()
        await runtime.stop()


async def test_health_check_reports_a_lost_qq_connection(env: Env) -> None:
    class Gateway:
        problem = ""

        def health(self) -> tuple[bool, str]:
            return not self.problem, self.problem

        async def stop(self) -> None:
            pass

    gateway = Gateway()
    env.runtime.gateway = gateway  # type: ignore[assignment]
    assert (await env.get("/healthz")) == {"ok": True, "service": "jianshanagent"}
    # 进程活着不算数：和 QQ 断了又连不回来，机器人什么都收不到，容器要显示成不健康
    gateway.problem = "和 QQ 的连接已经断开 300 秒"
    assert (await env.get("/healthz", 503)) == {
        "ok": False,
        "service": "jianshanagent",
        "problem": "和 QQ 的连接已经断开 300 秒",
    }


async def test_everything_needs_login_and_writes_need_the_header(env: Env) -> None:
    assert (await env.get("/healthz")) == {"ok": True, "service": "jianshanagent"}
    assert (await env.get("/api/session"))["authenticated"] is False
    assert "还没登录" in (await env.get("/api/overview", 401))["error"]
    assert (await env.client.post("/api/system/reload", headers=WRITE)).status == 401
    page = await env.client.get("/some/page")  # 页面本身不用登录（数据都在接口后面）
    assert page.status == 200 and page.content_type == "text/html"

    wrong = await env.client.post("/api/login", json={"token": "wrong-token-000000"})
    assert wrong.status == 401 and COOKIE not in wrong.cookies
    await env.login()
    assert (await env.get("/api/session"))["authenticated"] is True
    overview = await env.get("/api/overview")
    assert overview["stats"] is None and overview["status"]["database"] is False
    assert overview["status"]["dry_run"] is True and overview["status"]["gateway"] is None
    assert "chat" in overview["status"]["plugins"]

    # 改东西的请求要带自定义头：别的网站的表单带不了
    bare = await env.client.post("/api/system/reload")
    assert bare.status == 403
    assert (await env.send("POST", "/api/system/reload"))["status"]["generation"] == 2
    assert (await env.get("/api/nope", 404))["error"] == "没有这个接口"
    # 没有数据库：要查库的页面如实说，不是报一个看不懂的错
    assert "数据库" in (await env.get("/api/sessions", 503))["error"]

    await env.client.post("/api/logout", headers=WRITE)
    assert (await env.get("/api/session"))["authenticated"] is False


def test_session_cookie_is_signed_expires_and_login_locks(tmp_path: Path) -> None:
    auth = Auth(TOKEN, session_seconds=100, max_failures=3, lock_seconds=60)
    cookie = auth.issue(now=1000)
    assert auth.valid(cookie, now=1099) and not auth.valid(cookie, now=1101)
    issued, expires, signature = cookie.split(".")
    assert (issued, expires) == ("1000", "1100")
    # 改了期限、改了签发时间：签名都对不上
    assert not auth.valid(f"{issued}.{int(expires) + 999}.{signature}", now=1000)
    assert not auth.valid(f"{int(issued) + 999}.{expires}.{signature}", now=1000)
    assert not auth.valid(cookie[:-1] + ("0" if cookie[-1] != "0" else "1"), now=1000)
    assert not Auth("another-token-123456").valid(cookie, now=1000)  # 换了令牌：旧的全部失效
    assert not auth.valid(None) and not auth.valid("garbage")

    for _ in range(3):
        assert auth.check("wrong-guess-000000", "10.0.0.9", now=2000) is False
    assert auth.locked_for("10.0.0.9", now=2001) == pytest.approx(59)
    assert auth.check(TOKEN, "10.0.0.9", now=2001) is False  # 锁着的时候对的也不认
    assert auth.check(TOKEN, "10.0.0.8", now=2001) is True  # 别的地址不受影响
    assert auth.check(TOKEN, "10.0.0.9", now=2061) is True  # 过了就解锁

    # 令牌：secrets.yaml 里没填就生成一个存成文件（权限 600），之后一直用它
    config = load_config(REPO_CONFIG)
    config.secrets = {}
    path = tmp_path / "admin" / "token"
    token, source = load_token(config, str(path))
    assert source == "generated" and len(token) >= 24
    assert path.stat().st_mode & 0o777 == 0o600 and path.read_text().strip() == token
    assert load_token(config, str(path)) == (token, "file")
    config.secrets = {"admin": {"token": TOKEN}}
    assert load_token(config, str(path)) == (TOKEN, "secrets")
    config.secrets = {"admin": {"token": "short"}}
    with pytest.raises(ValueError, match="太短"):
        load_token(config, str(path))


class _Log:
    """接住 auth.py 打的日志：只记事件名和带的字段。"""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.seen.append((event, fields))

    @property
    def events(self) -> list[str]:
        return [event for event, _ in self.seen]


def test_logging_out_revokes_every_cookie_issued_before_it(tmp_path: Path) -> None:
    state = tmp_path / "admin" / SESSIONS_FILE
    assert sessions_file(str(tmp_path / "admin" / "token")) == state  # 和令牌文件同一个目录
    auth = Auth(TOKEN, session_seconds=1000, state_file=state)
    copied = auth.issue(now=5000)  # 被人抄走的那一份
    other = auth.issue(now=5003)  # 另一个浏览器上登录的
    assert auth.valid(copied, now=5004) and auth.valid(other, now=5004)
    assert not state.exists()  # 没退出过就没有这个文件

    # 原来退出登录只是让浏览器把 Cookie 删掉，抄走的那份到期前（30 天）一直能用
    assert auth.revoke(now=5010) is True
    assert not auth.valid(copied, now=5011) and not auth.valid(other, now=5011)
    assert state.stat().st_mode & 0o777 == 0o600 and state.read_text().strip() == "5011"
    assert [item.name for item in state.parent.iterdir()] == [SESSIONS_FILE]  # 临时文件没留下
    fresh = auth.issue(now=5020)
    assert auth.valid(fresh, now=5021)

    # 机器人重启：退出之前发的还是不算；之后发的、没退出过的照样有效，不用重新登录
    restarted = Auth(TOKEN, session_seconds=1000, state_file=state)
    assert not restarted.valid(copied, now=5030) and not restarted.valid(other, now=5030)
    assert restarted.valid(fresh, now=5030) and not restarted.valid(fresh, now=6021)  # 到期照旧

    # 老格式的 Cookie（到期时间.签名，没有签发时间）：签名是真的也不认，重新登录一次
    key = hashlib.sha256(b"jianshanagent-admin-session:" + TOKEN.encode()).digest()
    old = "6000." + hmac.new(key, b"6000", hashlib.sha256).hexdigest()
    assert not restarted.valid(old, now=5030) and not Auth(TOKEN).valid(old, now=5030)

    # 不给文件（只记在内存里）也能退出，只是重启就忘
    memory = Auth(TOKEN, session_seconds=1000)
    cookie = memory.issue(now=5000)
    assert memory.revoke(now=5001) is False and not memory.valid(cookie, now=5002)


def test_logout_and_login_within_the_same_second(tmp_path: Path) -> None:
    state = tmp_path / SESSIONS_FILE
    auth = Auth(TOKEN, session_seconds=1000, state_file=state)
    # 时间只记到秒。同一秒里：登录 → 退出 → 再登录。旧的要作废，新的要有效——
    # 只比「签发时间早于退出时间」的话两个分不开（都是 7000），不是都放过就是都作废
    first = auth.issue(now=7000.1)
    auth.revoke(now=7000.5)
    second = auth.issue(now=7000.9)
    assert not auth.valid(first, now=7000.95) and auth.valid(second, now=7000.95)
    # 还在这一秒里又来一轮
    auth.revoke(now=7000.96)
    third = auth.issue(now=7000.99)
    assert not auth.valid(first, now=7000.99) and not auth.valid(second, now=7000.99)
    assert auth.valid(third, now=7000.99)
    # 签发时间被推到了那条线上，期限还是从真正的现在算起
    assert third.split(".")[:2] == ["7002", "8000"] and state.read_text().strip() == "7002"

    # 这一秒里重启：结论不变，重启后马上登录发的也有效
    restarted = Auth(TOKEN, session_seconds=1000, state_file=state)
    assert restarted.valid(third, now=7000.99) and not restarted.valid(second, now=7000.99)
    assert restarted.valid(restarted.issue(now=7000.99), now=7001)

    # 服务器的钟被往回拨了：手里的 Cookie 签发时间比「现在」还晚，退出登录照样要作废它。
    # 重启过也一样——退出登录的请求带着它，验它的时候就记住了它的签发时间
    late = auth.issue(now=9000)
    for instance in (auth, Auth(TOKEN, session_seconds=1000, state_file=tmp_path / "another")):
        assert instance.valid(late, now=8000)
        instance.revoke(now=8000)
        assert not instance.valid(late, now=8001)
        assert instance.valid(instance.issue(now=8001), now=8002)


def test_a_sessions_file_that_cannot_be_read_or_written_does_not_break_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _Log()
    monkeypatch.setattr(auth_module, "logger", log)
    state = tmp_path / "admin" / SESSIONS_FILE
    earlier = Auth(TOKEN, session_seconds=1000).issue(now=7990)  # 这次启动之前发的

    # 读不了（内容坏了）：不知道哪些作废过，当作刚退出了一次——之前发的都不算，重新登录就行；
    # 文件顺手重写好，下次重启不用再登录
    state.parent.mkdir()
    state.write_text("这不是一个时间\n", encoding="utf-8")
    auth = Auth(TOKEN, session_seconds=1000, state_file=state, now=8000)
    assert log.events == ["admin_sessions_file_unreadable"]
    assert not auth.valid(earlier, now=8000)
    assert auth.check(TOKEN, "10.0.0.1", now=8000) is True  # 登录没有坏
    cookie = auth.issue(now=8000)
    assert auth.valid(cookie, now=8001) and state.read_text().strip() == "8001"
    assert Auth(TOKEN, session_seconds=1000, state_file=state, now=8005).valid(cookie, now=8006)
    assert log.events == ["admin_sessions_file_unreadable"]  # 修好了，没有再报

    # 写不了：这次退出在这个进程里照样算数，打一条警告；重启后就忘了（作废的又能用，如实记下来）
    def refuse(*_args: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(auth_module.os, "replace", refuse)
    assert auth.revoke(now=8010) is False
    assert log.events[-1] == "admin_sessions_file_not_saved"
    assert not auth.valid(cookie, now=8011) and state.read_text().strip() == "8001"
    assert [item.name for item in state.parent.iterdir()] == [SESSIONS_FILE]  # 半截的临时文件删了
    again = auth.issue(now=8012)
    assert auth.valid(again, now=8013)
    assert Auth(TOKEN, session_seconds=1000, state_file=state, now=8020).valid(cookie, now=8021)
    monkeypatch.undo()
    monkeypatch.setattr(auth_module, "logger", log)

    # 又读不了又写不了（那个位置上是个目录）：每次启动都要重新登录一次，但登录本身是好的
    broken = tmp_path / "broken" / SESSIONS_FILE
    broken.mkdir(parents=True)
    log.seen.clear()
    stuck = Auth(TOKEN, session_seconds=1000, state_file=broken, now=9000)
    assert log.events == ["admin_sessions_file_unreadable", "admin_sessions_file_not_saved"]
    assert not stuck.valid(again, now=9000) and stuck.check(TOKEN, "10.0.0.1", now=9000) is True
    relogin = stuck.issue(now=9000)
    assert stuck.valid(relogin, now=9001)
    assert stuck.revoke(now=9002) is False and not stuck.valid(relogin, now=9003)

    # 日志里只有文件在哪、出了什么问题：没有令牌，也没有 Cookie
    logged = repr(log.seen)
    assert str(broken) in logged
    assert TOKEN not in logged and not any(c in logged for c in (cookie, again, relogin))


async def test_logging_out_over_the_api_kills_a_copied_cookie(env: Env, tmp_path: Path) -> None:
    await env.login()
    home = env.client.make_url("/")
    copied = env.client.session.cookie_jar.filter_cookies(home)[COOKIE].value
    replay = {"Cookie": f"{COOKIE}={copied}"}
    # 这里令牌写在 secrets.yaml 里（没有令牌文件），退出的时间点照样存在 token_file 那个目录
    state = tmp_path / "admin" / SESSIONS_FILE
    # 另一处拿着抄来的 Cookie（不带这边浏览器的 Cookie 罐）
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as elsewhere:

        async def status(path: str, headers: dict[str, str]) -> int:
            async with elsewhere.get(env.client.make_url(path), headers=headers) as response:
                return response.status

        assert await status("/api/overview", replay) == 200

        # 没登录的人调退出登录：什么都不作废（不然随便谁都能把管理员踢下线）
        async with elsewhere.post(env.client.make_url("/api/logout"), headers=WRITE) as response:
            assert response.status == 200
        assert await status("/api/overview", replay) == 200 and not state.exists()

        # 管理员退出登录：抄走的那份马上不能用了
        await env.client.post("/api/logout", headers=WRITE)
        assert await status("/api/overview", replay) == 401
        assert (await env.get("/api/session"))["authenticated"] is False
        assert state.stat().st_mode & 0o777 == 0o600

        # 重新登录照常；机器人重启（重新挂一遍后台）后：新的还有效，抄走的还是不行
        await env.login()
        fresh = env.client.session.cookie_jar.filter_cookies(home)[COOKIE].value
        restarted = TestClient(TestServer(env.runtime.build_app()))
        await restarted.start_server()
        try:
            for value, expected in ((fresh, 200), (copied, 401)):
                url = restarted.make_url("/api/overview")
                async with elsewhere.get(url, headers={"Cookie": f"{COOKIE}={value}"}) as response:
                    assert response.status == expected
        finally:
            await restarted.close()


async def test_config_edits_apply_at_once_and_bad_ones_are_rolled_back(env: Env) -> None:
    await env.login()
    listing = await env.get("/api/config")
    by_name = {item["name"]: item for item in listing["files"]}
    assert by_name["plugin.wiki_query"]["mode"] == "reload" and by_name["app"]["mode"] == "restart"
    assert by_name["persona/persona.default"]["group"] == "人设"
    assert listing["state"]["writable"] is True and listing["state"]["overrides"] == []
    assert not any("secret" in name for name in by_name)  # 凭据不在这里

    detail = await env.get("/api/config/plugin.wiki_query")
    assert detail["description"].startswith("wiki_query 插件")
    fields = {field["key"]: field for field in detail["schema"]}
    assert fields["commands"]["kind"] == "group" and "wiki:" in detail["source"]

    # 改一项：马上生效，覆写文件里只有改的那一项
    edited = json.loads(json.dumps(detail["effective"]))
    edited["commands"]["lookup"]["name"] = "搜"
    saved = await env.send("PUT", "/api/config/plugin.wiki_query", {"values": edited})
    assert saved["saved"] == ["plugin.wiki_query"] and saved["needs_restart"] == []
    assert "搜" in env.runtime.host.commands and "查" not in env.runtime.host.commands
    assert env.runtime.generation == 2
    stored = (env.overrides / "plugin.wiki_query.yaml").read_text("utf-8")
    assert "name: 搜" in stored and "api_url" not in stored
    again = await env.get("/api/config/plugin.wiki_query")
    assert again["overridden"] == 1 and again["override"] == {
        "commands": {"lookup": {"name": "搜"}}
    }
    assert again["base"]["commands"]["lookup"]["name"] == "查"

    # 写错了（不是合法的正则）：装不起来 → 撤回，原来的照常运行
    safety = (await env.get("/api/config/plugin.safety"))["effective"]
    safety["outbound"]["block_patterns"] = ["("]
    rejected = await env.send("PUT", "/api/config/plugin.safety", {"values": safety}, 409)
    assert "已撤回" in rejected["error"]
    assert not (env.overrides / "plugin.safety.yaml").exists()
    assert env.runtime.generation == 2 and "搜" in env.runtime.host.commands

    # 对话入口用的那一档没了：也是当场拦下来，不是等有人说话才报错
    llm = (await env.get("/api/config/core.llm"))["effective"]
    del llm["profiles"]["chat"]
    llm["default_profile"] = "quick"
    rejected = await env.send("PUT", "/api/config/core.llm", {"values": llm}, 409)
    assert "chat" in rejected["error"] and not (env.overrides / "core.llm.yaml").exists()

    # 改回默认值：覆写文件删掉
    await env.send("PUT", "/api/config/plugin.wiki_query", {"values": again["base"]})
    assert not (env.overrides / "plugin.wiki_query.yaml").exists()
    assert "查" in env.runtime.host.commands and env.runtime.generation == 3

    await env.send("PUT", "/api/config/plugin.nope", {"values": {}}, 404)
    await env.send("PUT", "/api/config/plugin.safety", {"values": "不是键值结构"}, 400)


async def test_endpoints_keys_and_the_admins_own_switch_cannot_be_changed_from_the_web(
    env: Env,
) -> None:
    await env.login()

    async def refused(name: str, change: Any, mentions: str) -> None:
        values = (await env.get(f"/api/config/{name}"))["effective"]
        change(values)
        rejected = await env.send("PUT", f"/api/config/{name}", {"values": values}, 400)
        assert mentions in rejected["error"] and "不能在网页上改" in rejected["error"]
        assert not (env.overrides / f"{name}.yaml").exists()  # 什么都没存

    # 密钥是作为请求头发往这些地址的：能在网页上改地址，登录了后台的人就能让机器人
    # 把密钥发到自己的服务器上（改完点一下「测试模型」就到手）——凭据不该能从网页上拿到
    await refused("core.llm", lambda v: v.update(base_url="https://evil.test/v1"), "base_url")
    await refused(
        "plugin.wiki_query",
        lambda v: v["wiki"].update(api_url="https://evil.test/api.php"),
        "api_url",
    )
    await refused(
        "plugin.wiki_query", lambda v: v["wiki"].update(auth_header="X-Leak"), "wiki.auth_header"
    )
    # 用哪个密钥也不能改：把联网搜索的密钥指到模型的密钥上，再配上自己的地址，一样能带出去
    await refused(
        "core.websearch",
        lambda v: v["providers"]["mimo"].update(secret="llm"),
        "providers.mimo.secret",
    )

    def new_provider(values: dict[str, Any]) -> None:
        values["providers"]["mine"] = {"base_url": "https://evil.test/v1", "secret": "llm"}

    await refused("core.embedding", new_provider, "providers.mine.base_url")
    # 会把后台自己关在外面的两处：关掉后台；改健康检查的端口（容器的端口映射写死了 8080）
    await refused("core.admin", lambda v: v.update(enabled=False), "enabled")
    await refused("app", lambda v: v["healthcheck"].update(port=9000), "healthcheck")
    assert env.runtime.generation == 1

    # 同一份配置里别的项照常能改
    llm = (await env.get("/api/config/core.llm"))["effective"]
    llm["timeout_seconds"] = 120
    assert (await env.send("PUT", "/api/config/core.llm", {"values": llm}))["saved"] == ["core.llm"]
    assert env.runtime.config.get("core.llm")["base_url"].startswith("https://token-plan")


def _locked_fields(fields: list[dict[str, Any]]) -> set[str]:
    """表单结构里标成只读的项（一组整个锁着的只算那一组，不再往里数）。"""
    found: set[str] = set()
    for field in fields:
        if field["locked"]:
            found.add(".".join(field["path"]))
        else:
            found |= _locked_fields(field.get("children") or [])
    return found


async def test_the_page_is_told_which_items_cannot_be_changed_from_the_web(env: Env) -> None:
    """哪些项不能在网页上改，是接口告诉页面的（页面不自己抄一份名单），和保存时拒绝的是同一批。"""
    await env.login()
    expected = {
        "core.llm": {"base_url"},
        "core.websearch": {
            "providers.gitee_ai.base_url",
            "providers.gitee_ai.secret",
            "providers.mimo.base_url",
            "providers.mimo.secret",
        },
        "core.embedding": {
            "providers.gitee.base_url",
            "providers.gitee.secret",
            "providers.local.base_url",
            "providers.local.secret",
        },
        "plugin.wiki_query": set(),
        "core.wiki": {"wikis"},
        "core.storage": {"postgres.dsn_env", "redis.url_env"},
        "core.admin": {"enabled", "token_file"},
        "app": {"healthcheck"},
        "plugin.chat": set(),
    }
    for name, locked in expected.items():
        detail = await env.get(f"/api/config/{name}")
        assert _locked_fields(detail["schema"]) == locked, name
        # 标成只读的每一项：真去改它，保存时确实被拒绝（两边用的是同一个判断）
        for path in sorted(locked):
            values = json.loads(json.dumps(detail["effective"]))
            node, keys = values, path.split(".")
            for key in keys[:-1]:
                node = node[key]
            node[keys[-1]] = {"port": 1} if isinstance(node[keys[-1]], dict) else "改了"
            rejected = await env.send("PUT", f"/api/config/{name}", {"values": values}, 400)
            assert path in rejected["error"], (name, path)
        # 规则本身也给页面：这次编辑里新加出来的项（比如新加一个向量模型的服务商）照它判断
        assert "base_url" in detail["locked"]["keys"] and "secret" in detail["locked"]["keys"]
    assert (await env.get("/api/config/app"))["locked"]["paths"] == [["healthcheck"]]
    assert (await env.get("/api/config/core.admin"))["locked"]["paths"] == [["enabled"]]
    assert (await env.get("/api/config/core.llm"))["locked"]["paths"] == []
    assert env.runtime.generation == 1 and not list(env.overrides.glob("*.yaml"))

    # 模型页上也显示接口地址：同样告诉页面它不能改
    models = await env.get("/api/models")
    assert models["base_url_locked"] is True and models["base_url"].startswith("https://")


async def test_what_can_be_reached_without_logging_in_takes_no_big_or_odd_input(env: Env) -> None:
    # 整个服务的请求体上限为了网页对话传图放到了 64 MB；登录接口不用登录就能调，
    # 不能让谁都能叫进程把几十 MB 读进内存
    big = await env.client.post("/api/login", data=b'{"token": "' + b"x" * 100_000 + b'"}')
    assert big.status == 413
    # 嵌套极深的 JSON、不是 ASCII 的「数字」Cookie：照常拒绝，不是报 500 再打一段堆栈
    deep = await env.client.post("/api/login", data=b"[" * 1900 + b"]" * 1900)
    assert deep.status == 400
    odd = await env.client.get("/api/overview", headers={"Cookie": f"{COOKIE}=\u0661\u0662.abc"})
    assert odd.status == 401
    await env.login()  # 正常的登录不受影响


async def test_a_bad_value_in_a_restart_only_file_does_not_stop_the_bot_from_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, overrides = _copy_config(tmp_path), tmp_path / "overrides"
    (root / "secrets.yaml").write_text(
        f"llm:\n  api_key: tp-test\nadmin:\n  token: {TOKEN}\n", encoding="utf-8"
    )
    overrides.mkdir()
    # 「要重启才生效」的配置保存时没有重新装配这一关：写错的值要到重启时才发作。
    # 原来是启动直接抛异常、容器反复重启，管理后台也进不去，只能上服务器删文件
    (overrides / "gateway.qq.yaml").write_text(
        "reply:\n  passive:\n    c2c_max_replies: 四\n", encoding="utf-8"
    )
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(root))
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", str(overrides))
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.setattr("jianshanagent.runtime.setup_logging", lambda **_kwargs: None)
    runtime = Runtime(dry_run=True)
    await runtime.prepare()
    try:
        assert "invalid literal" in runtime.overrides_ignored and runtime.config.overrides == {}
        assert runtime._boot.max_replies == {"c2c": 4, "group": 5}  # 按默认值起来了
        assert runtime._boot.health_port == 8080
        runtime.build_app()  # 后台照样挂得上
    finally:
        await runtime.stop()


async def test_some_files_only_take_effect_after_a_restart(env: Env) -> None:
    await env.login()
    app = (await env.get("/api/config/app"))["effective"]
    app["log"]["level"] = "DEBUG"
    saved = await env.send("PUT", "/api/config/app", {"values": app})
    assert saved["needs_restart"] == ["app"]
    assert env.runtime.generation == 1  # 没有重新装配
    state = (await env.get("/api/config"))["state"]
    assert state["pending_restart"] == ["app"] and state["pending_reload"] == []

    # 几份一起存：要么都成要么都撤回
    files = {
        "core.router": {**(await env.get("/api/config/core.router"))["effective"]},
        "plugin.safety": (await env.get("/api/config/plugin.safety"))["effective"],
    }
    files["core.router"]["command_prefixes"] = ["#"]
    files["plugin.safety"]["outbound"]["block_patterns"] = ["["]
    await env.send("PUT", "/api/config", {"files": files}, 409)
    assert not (env.overrides / "core.router.yaml").exists()
    assert env.runtime.router.prefixes == ["/", "！", "!"]

    # 处理记录的设置写错了类型：也是「装不起来 → 撤回」，不能换了一半才报错
    # （原来这时新的一套已经换上去了，接口却说「已撤回，仍按原来的配置运行」）
    before = env.runtime.host
    trace_cfg = (await env.get("/api/config/core.trace"))["effective"]
    trace_cfg["prompts"] = "x"
    router_cfg = {**(await env.get("/api/config/core.router"))["effective"]}
    router_cfg["command_prefixes"] = ["#"]
    both = {"core.trace": trace_cfg, "core.router": router_cfg}
    rejected = await env.send("PUT", "/api/config", {"files": both}, 409)
    assert "已撤回" in rejected["error"]
    assert env.runtime.host is before and env.runtime.generation == 1
    assert env.runtime.router.prefixes == ["/", "！", "!"]

    restart = await env.send("POST", "/api/system/restart")
    assert restart == {"restarting": True}


async def test_overrides_that_break_startup_are_ignored_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, overrides = tmp_path / "config", tmp_path / "overrides"
    shutil.copytree(REPO_CONFIG, root, ignore=shutil.ignore_patterns("local", "secrets.yaml"))
    (root / "secrets.yaml").write_text("llm:\n  api_key: tp-test\n", encoding="utf-8")
    overrides.mkdir()
    (overrides / "plugin.safety.yaml").write_text(
        "outbound:\n  block_patterns: ['(']\n", encoding="utf-8"
    )
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(root))
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", str(overrides))
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.setattr("jianshanagent.runtime.setup_logging", lambda **_kwargs: None)
    runtime = Runtime(dry_run=True)
    await runtime.prepare()  # 网页上改错的配置不能让机器人起不来：退回默认值
    try:
        assert runtime.overrides_ignored and runtime.config.overrides == {}
        assert runtime.config.plugin("safety")["outbound"]["block_patterns"] == []
        assert runtime.pending_reload(load_config()) == ["plugin.safety"]
        with pytest.raises(Exception, match="unterminated|missing"):
            await runtime.reload()  # 还没修好：重新装配照样失败，仍按默认值运行
        (overrides / "plugin.safety.yaml").unlink()
        await runtime.reload()
        assert runtime.overrides_ignored == "" and runtime.generation == 2
    finally:
        await runtime.stop()


async def test_plugins_can_be_switched_off_and_on(env: Env) -> None:
    await env.login()
    listing = await env.get("/api/plugins")
    plugins = {item["id"]: item for item in listing["plugins"]}
    assert listing["enabled"] == listing["default"] and "memory" in listing["enabled"]
    assert plugins["memory"]["loaded"] and plugins["memory"]["active"]
    assert {tool["name"] for tool in plugins["memory"]["tools"]} >= {"memory_save"}
    assert [c["name"] for c in plugins["wiki_query"]["commands"]] == ["查"]
    assert plugins["chat"]["hooks"]["chat"] is True and plugins["chat"]["config"] == "plugin.chat"
    assert plugins["knowledge"]["active"] is False  # 没有数据库：加载了但没起作用
    assert plugins["memory"]["summary"].startswith("记忆插件")

    without = [name for name in listing["enabled"] if name != "memory"]
    await env.send("PUT", "/api/plugins", {"enabled": without})
    assert "memory_save" not in env.runtime.host.tools and "记忆" not in env.runtime.host.commands
    after = {item["id"]: item for item in (await env.get("/api/plugins"))["plugins"]}
    assert after["memory"]["enabled"] is False and after["memory"]["exists"] is True

    await env.send("PUT", "/api/plugins", {"enabled": listing["default"]})
    assert "memory_save" in env.runtime.host.tools
    assert not (env.overrides / "core.plugins.yaml").exists()
    await env.send("PUT", "/api/plugins", {"enabled": [*without, "no_such_plugin"]}, 400)


async def test_plugins_added_to_the_defaults_later_switch_on_by_themselves(env: Env) -> None:
    """后台动过插件开关以后，以后的发布新加的默认插件照样自动启用；后台关掉的不会被重新打开。"""
    await env.login()
    plugins_yaml = env.root / "core.plugins.yaml"
    current = plugins_yaml.read_text("utf-8")
    older = "".join(line for line in current.splitlines(True) if "- chatlog" not in line)
    assert older != current

    async def enabled() -> list[str]:
        listing = await env.get("/api/plugins")
        assert listing["enabled"] == env.runtime.config.get("core.plugins")["enabled"]
        assert [p["id"] for p in listing["plugins"] if p["loaded"]] == listing["enabled"]
        return list(listing["enabled"])

    # 上一个版本：默认清单里还没有 chatlog。在后台把 memory 关掉
    plugins_yaml.write_text(older, "utf-8")
    await env.runtime.reload()
    before = await enabled()
    assert "chatlog" not in before and "memory" in before
    await env.send("PUT", "/api/plugins", {"enabled": [n for n in before if n != "memory"]})
    stored = yaml.safe_load((env.overrides / "core.plugins.yaml").read_text("utf-8"))
    assert stored[ENABLED_BASELINE] == before and "memory" not in stored["enabled"]
    # 记账的那一项不出现在配置页上，也不算「改了一项」之外的改动
    detail = await env.get("/api/config/core.plugins")
    assert list(detail["effective"]) == ["enabled"] and detail["overridden"] == 1
    assert [field["key"] for field in detail["schema"]] == ["enabled"]

    # 发布新版本：默认清单里加了 chatlog。重新装配（重启走的是同一条读配置的路）
    plugins_yaml.write_text(current, "utf-8")
    assert env.runtime.pending_reload(load_config()) == ["core.plugins"]
    await env.runtime.reload()
    after = await enabled()
    # 新加的启用了，排在 chat 后面
    assert after == [*[n for n in before if n != "memory"], "chatlog"]
    assert "memory_save" not in env.runtime.host.tools  # 后台关掉的还关着

    # 默认清单里删掉的插件：覆写里还列着它（代码也已经没有了），装配不报错
    (env.overrides / "core.plugins.yaml").write_text(
        yaml.safe_dump(
            {
                "enabled": ["safety", "retired", "wiki_query", "chat"],
                ENABLED_BASELINE: ["safety", "retired", "wiki_query", "memory", "chat"],
            }
        ),
        "utf-8",
    )
    await env.runtime.reload()
    assert await enabled() == ["safety", "wiki_query", "knowledge", "research", "chat", "chatlog"]

    # 在配置页上整份保存 core.plugins 也一样记账
    await env.send(
        "PUT", "/api/config/core.plugins", {"values": {"enabled": ["safety", "wiki_query", "chat"]}}
    )
    stored = yaml.safe_load((env.overrides / "core.plugins.yaml").read_text("utf-8"))
    assert stored[ENABLED_BASELINE] == load_config().base_files["core.plugins"]["enabled"]
    assert await enabled() == ["safety", "wiki_query", "chat"]


async def test_a_plugin_list_saved_by_an_older_version_is_kept_and_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, overrides = _copy_config(tmp_path), tmp_path / "overrides"
    (root / "secrets.yaml").write_text(
        f"llm:\n  api_key: tp-test\nadmin:\n  token: {TOKEN}\n", encoding="utf-8"
    )
    overrides.mkdir()
    # 旧版本的后台存的覆写：只有一整份启用清单
    legacy = overrides / "core.plugins.yaml"
    legacy.write_text("enabled:\n- safety\n- wiki_query\n- chat\n", encoding="utf-8")
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(root))
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", str(overrides))
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.setattr("jianshanagent.runtime.setup_logging", lambda **_kwargs: None)
    defaults = load_config().base_files["core.plugins"]["enabled"]

    runtime = Runtime(dry_run=True)
    await runtime.prepare()
    try:
        # 启用的和旧版本读出来的一模一样
        assert runtime.config.get("core.plugins")["enabled"] == ["safety", "wiki_query", "chat"]
        assert runtime.overrides_ignored == ""
        runtime.build_app()  # 管理后台启动时给它补记上「现在的默认清单」
        assert yaml.safe_load(legacy.read_text("utf-8")) == {
            "enabled": ["safety", "wiki_query", "chat"],
            ENABLED_BASELINE: defaults,
        }
        # 补记不改变启用哪些：不会冒出「有改动还没生效」
        assert runtime.pending_reload(load_config()) == []

        # 补不成（覆写目录写不了）不拦启动
        legacy.write_text("enabled:\n- safety\n- wiki_query\n- chat\n", encoding="utf-8")
        monkeypatch.setattr(
            "jianshanagent.admin.note_plugin_baseline",
            lambda _config: (_ for _ in ()).throw(PermissionError(13, "只读")),
        )
        runtime.build_app()
        assert ENABLED_BASELINE not in legacy.read_text("utf-8")
    finally:
        await runtime.stop()


class _FakeQueries:
    """只记下接口传给查询的参数（这份测试里没有数据库）。"""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def sessions(self, **kwargs: Any) -> dict[str, Any]:
        self.seen.append(kwargs)
        return {"total": 0, "items": []}

    async def chats(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.seen.append(kwargs)
        return []


async def test_session_and_chat_lists_take_a_time_window(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.login()
    fake = _FakeQueries()
    monkeypatch.setattr(Admin, "queries", property(lambda _self: fake))

    # 不说就是默认的时间范围（不再每次把几张表整个算一遍）；接口把实际用的天数告诉页面
    assert await env.get("/api/sessions") == {"total": 0, "items": [], "days": SESSION_DAYS}
    assert await env.get("/api/chatlog/chats") == {"items": [], "days": CHAT_DAYS}
    assert (await env.get("/api/sessions?days=0&q=abc&limit=20"))["days"] == 0  # 0 = 不限时间
    assert (await env.get("/api/chatlog/chats?days=30"))["days"] == 30
    assert (await env.get("/api/chatlog/chats?days=-5"))["days"] == 0
    await env.get("/api/sessions?days=abc", 400)
    assert fake.seen == [
        {"limit": 50, "offset": 0, "search": None, "days": SESSION_DAYS},
        {"days": CHAT_DAYS},
        {"limit": 20, "offset": 0, "search": "abc", "days": 0},
        {"days": 30},
        {"days": 0},
    ]
    assert 0 < CHAT_DAYS <= SESSION_DAYS <= 90


async def test_backup_state_is_looked_up_at_most_once_a_minute_and_flags_a_missing_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    state: dict[str, Any] = {"last": None, "last_ok_at": None, "since": now - timedelta(hours=2)}
    lookups: list[bool] = []
    broken = False

    class Queries:
        async def backup_state(self) -> dict[str, Any]:
            lookups.append(broken)
            if broken:
                raise OSError("连不上")
            return dict(state)

    class Stub:
        db: Any = object()

    monkeypatch.setattr(Admin, "queries", property(lambda _self: Queries()))
    admin = Admin(Stub(), AdminSettings(), None, "secrets")  # type: ignore[arg-type]

    async def look(*, again: bool = True) -> dict[str, Any] | None:
        if again:
            admin._backup_checked = None  # 当作过了一分钟
        await admin.refresh_backup()
        return admin.backup()

    assert admin.backup() is None  # 还没查过
    # 刚部署、第一次备份还没到点：没有记录，但不算出了问题
    assert await look() == {**state, "warn_hours": 26.0, "overdue": False}
    # 页面每隔几秒来问一次，一分钟里只查一次库
    assert await look(again=False) is not None and lookups == [False]

    # 有记录的地方过了 26 小时一次成功的都没有：提醒
    state["since"] = now - timedelta(hours=27)
    assert (await look())["overdue"] is True  # type: ignore[index]
    # 昨天的成功了，今天这次失败了：还没到 26 小时，先不提醒，但最近一次是失败的照实给出去
    state["last"] = {"finished_at": now, "ok": False, "detail": "pg_dump 没有成功"}
    state["last_ok_at"] = now - timedelta(hours=24)
    found = await look()
    assert found is not None and found["overdue"] is False and found["last"]["ok"] is False
    # 连着两天没成
    state["last_ok_at"] = now - timedelta(hours=48)
    assert (await look())["overdue"] is True  # type: ignore[index]

    # 数据库正好连不上：沿用上一次查到的，页面别的部分照常
    broken = True
    assert (await look())["overdue"] is True and lookups[-1] is True  # type: ignore[index]
    broken = False

    # 配成 0 = 不提醒；没有数据库 = 没有这一项
    admin.settings = AdminSettings(backup_warn_hours=0)
    assert (await look())["overdue"] is False  # type: ignore[index]
    Stub.db = None
    assert await look() is None


def test_backup_warning_hours_come_from_the_config() -> None:
    def settings(text: str) -> AdminSettings:
        files = {"core.admin": yaml.safe_load(text) or {}}
        return AdminSettings.from_config(AppConfig(root=Path("."), files=files, secrets={}))

    repo = (REPO_CONFIG / "core.admin.yaml").read_text("utf-8")
    assert settings(repo).backup_warn_hours == 26
    assert settings("").backup_warn_hours == 26  # 没写就是默认值
    assert settings("backup_warn_hours: 0").backup_warn_hours == 0
    assert settings("backup_warn_hours: -3").backup_warn_hours == 0


async def test_web_chat_goes_through_the_real_pipeline(env: Env) -> None:
    await env.login()
    seen: list[str] = []

    async def ping(message: InboundMessage, args: str) -> OutboundMessage:
        return OutboundMessage(content=f"pong {message.user_name} {args}", markdown=True)

    async def observe(message: InboundMessage) -> None:
        seen.append(message.content)

    async def chat(message: InboundMessage) -> OutboundMessage | None:
        if "炸" in message.content:
            raise RuntimeError("入口炸了")
        with trace.span("entry", "对话入口"):
            await message.progress("正在查…")
        return OutboundMessage(content=f"收到 {len(message.images)} 张图")

    host = env.runtime.host
    host.commands["ping"] = CommandSpec(name="ping", handler=ping, plugin_id="demo")
    host.observers.append(observe)
    env.runtime.router.set_chat_handler(chat)

    events = await env.chat({"text": "/ping 一下", "user": "阿岚"})
    assert [name for name, _ in events] == ["start", "reply", "done"]
    assert events[0][1]["session"] == "c2c:web-阿岚"
    assert events[1][1] == {"content": "pong 阿岚 一下", "markdown": True, "buttons": []}
    trace_id = events[2][1]["trace_id"]

    pixel = "data:image/png;base64,iVBORw0KGgo="
    events = await env.chat({"text": "看图", "images": [pixel], "scope": "group"})
    assert [name for name, _ in events] == ["start", "progress", "reply", "done"]
    assert events[0][1]["session"] == "group:web-group:user:web-admin"
    assert events[1][1] == {"content": "正在查…"} and events[2][1]["content"] == "收到 1 张图"

    events = await env.chat({"text": "炸了吗"})
    assert [name for name, _ in events] == ["start", "error", "done"]
    assert "入口炸了" in events[1][1]["message"]
    assert seen == []  # 网页对话不给旁听的插件看（不进群聊记录）

    response = await env.client.post("/api/chat", json={"text": "  "}, headers=WRITE)
    assert response.status == 400
    response = await env.client.post(
        "/api/chat", json={"text": "x", "images": ["not-an-image"]}, headers=WRITE
    )
    assert response.status == 400

    # 处理记录：没有数据库时在内存里也查得到刚才那几条
    listing = await env.get("/api/traces")
    assert listing["stored"] is False
    assert [(t["route"], t["status"], t["channel"]) for t in listing["items"]] == [
        ("chat", "error", "web"),
        ("chat", "replied", "web"),
        ("command", "replied", "web"),
    ]
    detail = await env.get(f"/api/traces/{trace_id}")
    assert detail["question"] == "/ping 一下" and detail["reply"] == "pong 阿岚 一下"
    assert [step["kind"] for step in detail["steps"]] == ["command"] and detail["calls"] == []
    await env.get("/api/traces/20200101-000000-ffffff", 404)

    reset = await env.send("POST", "/api/chat/reset", {"user": "阿岚"})
    assert reset["session"] == "c2c:web-阿岚"


async def test_models_page_and_logs(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.login()
    models = await env.get("/api/models")
    profiles = {item["name"]: item for item in models["profiles"]}
    assert models["enabled"] and models["running"] and models["default_profile"] == "chat"
    assert profiles["chat"]["thinking"] is True and "必须开思考" not in profiles["chat"]["model"]
    assert "对话" in profiles["chat"]["description"]
    roles = {role["id"]: role for role in models["roles"]}
    research = load_config(REPO_CONFIG).plugin("research")["profiles"]
    assert roles["analyst"]["profile"] == research["analyst"]
    assert roles["analyst"]["file"] == "plugin.research" and roles["analyst"]["profile"] in profiles
    assert roles["reviewer"]["path"] == ["profiles", "reviewer"] and roles["entry"]["file"] is None
    assert "web" not in roles  # 联网搜索不走大模型，没有档位可选
    assert models["web_search"] is load_config(REPO_CONFIG).get("core.websearch")["enabled"]

    llm = env.runtime.llm
    assert llm is not None
    purposes: list[str] = []

    async def fake_chat(messages: Any, *, profile: str) -> Any:
        from jianshanagent.core.llm import LLMResult, LLMUsage

        purposes.append(trace._purpose.get())
        return LLMResult(
            message={"role": "assistant", "content": "收到"},
            content="收到",
            reasoning="想了想",
            tool_calls=[],
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=12, completion_tokens=3),
            model="mimo-test",
        )

    monkeypatch.setattr(llm, "chat", fake_chat)
    tested = await env.send("POST", "/api/models/test", {"profile": "quick"})
    assert tested["ok"] is True and tested["content"] == "收到" and tested["prompt_tokens"] == 12
    assert purposes == ["test"]
    await env.send("POST", "/api/models/test", {"profile": "nope"}, 404)

    before = (await env.get("/api/logs"))["last"]
    LOG_BUFFER.add("warning", "something_happened", {"detail": "x"})
    logs = await env.get(f"/api/logs?after={before}")
    assert [(item["level"], item["event"]) for item in logs["items"]] == [
        ("warning", "something_happened")
    ]
    assert logs["last"] == before + 1

    system = await env.get("/api/system")
    secrets = {item["key"]: item["set"] for item in system["secrets"]}
    assert secrets == {"llm.api_key": True, "admin.token": True}  # 只说填没填
    assert TOKEN not in json.dumps(system) and "tp-test" not in json.dumps(system)
    assert system["environment"]["token_source"] == "secrets"
    # 没有数据库就没有备份的情况可说：不提醒
    assert system["status"]["backup"] is None and (await env.get("/api/status"))["backup"] is None
