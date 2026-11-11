"""管理后台的登录：一个令牌，登录后发一个带签名、有期限的 Cookie。

- 令牌：优先用 ``secrets.yaml`` 的 ``admin.token``；没填就在第一次启动时生成一个，存到
  ``core.admin.yaml`` 的 ``token_file``（权限 600）。令牌本身不进日志。
- Cookie 的内容是「签发时间 + 到期时间 + 用令牌派生的密钥签的名」，服务器上不逐个记它们，
  所以机器人重启后不用重新登录；换了令牌，以前发出去的 Cookie 全部失效。它是 HttpOnly、
  SameSite=Strict 的，网页里的脚本读不到，别的网站发起的请求也带不上。
- 退出登录 = 把以前发出去的 Cookie 全部作废（管理员只有一个，「全部登出」就是该有的意思）：
  服务器只记一个时间点，签发得比它早的都不算。这个时间点存在令牌文件旁边的一个小文件里
  （:data:`SESSIONS_FILE`），重启后还在。只让浏览器删 Cookie 不够：被人抄走的那份到期前都能用。
- 改东西的请求（非 GET）还要带一个自定义请求头：别的网站的表单带不了自定义头，
  而带自定义头的跨站请求浏览器会先来问（预检），这边不答应。
- 同一个地址连着输错几次就锁一会儿，防止试令牌。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

COOKIE = "jianshanagent_admin"
#: 改东西的请求必须带的请求头
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "jianshanagent-admin"
#: 「签发得比这个时间点早的 Cookie 都不算」记在哪个文件里（和令牌文件在同一个目录）
SESSIONS_FILE = "sessions_valid_from"
_MIN_TOKEN_CHARS = 12


def sessions_file(token_file: str) -> Path:
    """退出登录的那个时间点存在哪。令牌写在 secrets.yaml 里（没有令牌文件）时也放在那个目录。"""
    return Path(token_file).expanduser().parent / SESSIONS_FILE


def load_token(config: AppConfig, token_file: str) -> tuple[str, str]:
    """取登录令牌 →（令牌，它是哪来的）。来源：secrets / file / generated。"""
    configured = str((config.secrets.get("admin") or {}).get("token") or "").strip()
    if configured:
        if len(configured) < _MIN_TOKEN_CHARS:
            raise ValueError(f"secrets.yaml 的 admin.token 太短了，至少 {_MIN_TOKEN_CHARS} 个字符")
        return configured, "secrets"
    path = Path(token_file).expanduser()
    if path.is_file():
        saved = path.read_text(encoding="utf-8").strip()
        if len(saved) >= _MIN_TOKEN_CHARS:
            return saved, "file"
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先按 600 建文件再写，令牌任何时候都不会以别人能读的权限躺在盘上
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(path, 0o600)
    return token, "generated"


class Auth:
    def __init__(
        self,
        token: str,
        *,
        session_seconds: float = 30 * 86400,
        max_failures: int = 5,
        lock_seconds: float = 60.0,
        state_file: Path | None = None,
        now: float | None = None,
    ):
        """``state_file``：退出登录的时间点存在哪（见 :func:`sessions_file`）；
        不给就只记在内存里，重启就忘（测试用）。"""
        self._token = token
        self._key = hashlib.sha256(b"jianshanagent-admin-session:" + token.encode("utf-8")).digest()
        self.session_seconds = session_seconds
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        #: 各个地址连着输错了几次、最后一次是什么时候
        self._failures: dict[str, tuple[int, float]] = {}
        self._state_file = state_file
        #: 签发时间（整秒）比它早的 Cookie 一律不算。0 = 从没退出过登录
        self._valid_from = 0
        #: 见过的 Cookie 里最晚的签发时间：退出登录时那条线至少要划到它后面
        self._latest_issued = 0
        if state_file is not None:
            self._load_valid_from(time.time() if now is None else now)

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def _load_valid_from(self, now: float) -> None:
        """启动时把上次退出登录的时间点读回来。

        没有这个文件 = 从没退出过，以前发的 Cookie 照常有效。文件在但读不了（权限不对、内容坏了）
        就不知道哪些 Cookie 作废过：宁可当作刚刚退出了一次——这次启动之前发的都不算，重新登录
        一次就行——也不把作废过的放回来。然后试着把文件重写好，写成了下次重启就不用再登录。
        """
        path = self._state_file
        assert path is not None
        try:
            saved = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:  # 内容不是 ASCII 是 ValueError
            problem = type(exc).__name__
        else:
            if saved.isascii() and saved.isdigit():
                self._valid_from = int(saved)
                return
            problem = "内容不是一个时间点"
        # 日志里只有文件在哪、出了什么问题；令牌和 Cookie 不在这个文件里，也不进日志
        logger.warning("admin_sessions_file_unreadable", file=str(path), problem=problem)
        self._valid_from = int(now) + 1
        self._save_valid_from()

    def _save_valid_from(self) -> bool:
        """把时间点写到盘上，返回写成了没有。先写临时文件再改名：断电不会留下半个文件。"""
        path = self._state_file
        if path is None:
            return False
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{self._valid_from}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)  # 临时文件是上次没写完留下的话，权限以这次为准
            os.replace(temporary, path)
        except OSError as exc:
            # 没写成：这次退出在这个进程里照样算数，但重启后就忘了，作废的 Cookie 会重新有效
            logger.warning(
                "admin_sessions_file_not_saved",
                file=str(path),
                problem=f"{type(exc).__name__}: {exc.strerror or exc}",
            )
            with contextlib.suppress(OSError):
                temporary.unlink()
            return False
        return True

    def issue(self, now: float | None = None) -> str:
        """登录成功后发的 Cookie 内容：签发时间.到期时间.签名（两个时间都签在里面）。"""
        moment = time.time() if now is None else now
        # 签发时间不早于「之前的都不算」那条线：刚退出、同一秒里又登录，新发的这个要算数
        issued = max(int(moment), self._valid_from)
        self._latest_issued = max(self._latest_issued, issued)
        payload = f"{issued}.{int(moment + self.session_seconds)}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, cookie: str | None, now: float | None = None) -> bool:
        # 以前的 Cookie 只有「到期时间.签名」两段，没有签发时间，没法知道它是不是退出之前发的：
        # 一律不认，重新登录一次
        parts = cookie.split(".") if cookie else []
        if len(parts) != 3:
            return False
        issued, expires, signature = parts
        # 先挡掉不是 ASCII 的：阿拉伯文数字之类能过 isdigit()，到比较签名时才抛异常
        if not (cookie or "").isascii() or not (issued.isdigit() and expires.isdigit()):
            return False
        if not hmac.compare_digest(signature, self._sign(f"{issued}.{expires}")):
            return False
        self._latest_issued = max(self._latest_issued, int(issued))
        if int(issued) < self._valid_from:
            return False
        return int(expires) > (time.time() if now is None else now)

    def revoke(self, now: float | None = None) -> bool:
        """退出登录：在此之前发出去的 Cookie 全部作废。返回这件事有没有记到盘上。

        时间只精确到秒，所以那条线划在「现在」的下一秒：这一秒里已经发出去的都作废，
        之后再登录发的（签发时间被 :meth:`issue` 推到线上）照常有效。服务器的钟被往回拨过时，
        手里可能有签发时间比现在还晚的 Cookie，所以也要划到见过的最晚签发时间后面。
        """
        moment = time.time() if now is None else now
        self._valid_from = max(int(moment), self._valid_from, self._latest_issued) + 1
        return self._save_valid_from()

    def locked_for(self, client: str, now: float | None = None) -> float:
        """这个地址还要锁多少秒（没锁是 0）。"""
        count, last = self._failures.get(client, (0, 0.0))
        if count < self.max_failures:
            return 0.0
        remaining = last + self.lock_seconds - (time.time() if now is None else now)
        if remaining <= 0:
            self._failures.pop(client, None)
            return 0.0
        return remaining

    def check(self, candidate: str, client: str, now: float | None = None) -> bool:
        """令牌对不对。输错的记一次，输对的把之前的错清掉。锁着的时候一律算错。"""
        moment = time.time() if now is None else now
        if self.locked_for(client, moment) > 0:
            return False
        if hmac.compare_digest(candidate.encode("utf-8"), self._token.encode("utf-8")):
            self._failures.pop(client, None)
            return True
        count, _ = self._failures.get(client, (0, 0.0))
        self._failures[client] = (count + 1, moment)
        logger.warning("admin_login_failed", client=client, failures=count + 1)
        return False
