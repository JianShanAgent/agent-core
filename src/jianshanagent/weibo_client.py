"""固定微博只读端点、有限 HTTP 传输和独立的官网 viewer 身份检查。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from lxml import etree  # type: ignore[import-untyped]

from jianshanagent.weibo_http import _LOGIN_LOG_SCOPE

_MESSAGES = {
    "authentication_required": "微博登录已失效或无法确认，请重新同步登录",
    "risk_control": "微博要求人工验证，已停止采集",
    "rate_limited": "微博暂时限流，请稍后重试",
    "network": "微博网络请求失败，请稍后重试",
    "response_too_large": "微博响应超过安全限制，已停止采集",
    "schema_changed": "微博返回结构发生变化，已停止采集",
    "author_mismatch": "微博返回了其他作者的帖子，已停止采集",
    "pagination_stalled": "微博分页没有进展，已停止采集",
    "invalid_checkpoint": "微博续采断点无效，已停止采集",
    "invalid_settings": "微博采集配置无效",
    "detail_unavailable": "微博完整正文暂不可用，已保留已有资料",
    "row_too_large": "微博单条完整资料超过导入大小限制，已保留已有资料",
    "inaccessible": "微博讨论已删除或不可访问，已记录未核实项",
}
_MAX_BODY = 2 * 1024 * 1024
_MAX_REQUESTS = 100
_MIN_INTERVAL = 1.0
_RETRIES = 2
_GATE = asyncio.Lock()
_LAST_REQUEST = 0.0


class WeiboCollectError(Exception):
    """Only fixed, credential-free diagnostics may leave the collector."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _MESSAGES else "network"
        self.public_message = _MESSAGES[self.code]
        super().__init__(self.public_message)


class RequestBudgetReached(Exception):
    """Internal bounded-request budget; caller returns a resumable partial batch."""


def _viewer_config(value: dict[str, Any]) -> bool:
    uid, user = value.get("uid"), value.get("user")
    return bool(
        re.fullmatch(r"[1-9][0-9]{3,31}", str(uid or ""))
        and isinstance(user, dict)
        and str(user.get("id")) == str(uid)
    )


def _config_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate config fields")
    return result


def _bootstrap_suffix(script: str) -> bool:
    """Recognize but NEVER execute the observed static root-page loader."""
    if "$CONFIG" in script:
        return False
    match = re.fullmatch(
        r"\s*;?\s*const\s+(?P<variable>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"document\.createElement\(\s*(?P<tag_quote>['\"])script(?P=tag_quote)\s*\)\s*;\s*"
        r"(?P=variable)\.src\s*=\s*(?P<url_quote>['\"])(?P<url>https://[^'\"\\\s]{1,4096})"
        r"(?P=url_quote)\s*;\s*document\.body\.appendChild\(\s*(?P=variable)\s*\)\s*;\s*",
        script,
    )
    if match is None:
        return False
    try:
        url = urlsplit(match["url"])
        return bool(
            url.scheme == "https"
            and url.hostname
            and url.username is None
            and url.password is None
            and url.port in (None, 443)
            and not any(ord(char) <= 32 or ord(char) == 127 for char in match["url"])
        )
    except ValueError:
        return False


def _script_viewer(script: str) -> dict[str, Any] | None:
    """Recognize a literal assignment or fixed try/catch plus the known inert loader."""
    decoder = json.JSONDecoder(object_pairs_hook=_config_pairs)
    assignment = r"(?:var\s+)?(?:window\.)?\$CONFIG\s*=\s*"
    opening = re.match(r"\s*(?:(try)\s*\{\s*)?" + assignment, script)
    if opening is None:
        return None
    value, consumed = decoder.raw_decode(script[opening.end() :])
    if not isinstance(value, dict) or not _viewer_config(value):
        return None
    offset = opening.end() + consumed
    if opening[1] is None:
        return value if re.fullmatch(r"\s*;?\s*", script[offset:]) else None
    catch = re.match(
        r"\s*;?\s*\}\s*catch\s*\(\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\)\s*\{\s*",
        script[offset:],
    )
    if catch is None:
        return None
    offset += catch.end()
    if re.fullmatch(r"\s*\}\s*;?\s*", script[offset:]):
        return value  # Existing empty-catch form; no alternate assignment.
    fallback = re.match(r"window\.\$CONFIG\s*=\s*", script[offset:])
    if fallback is None:
        return None
    offset += fallback.end()
    anonymous, consumed = decoder.raw_decode(script[offset:])
    offset += consumed
    if not isinstance(anonymous, dict) or anonymous != {}:
        return None
    closing = re.match(r"\s*;?\s*\}", script[offset:])
    if closing is None:
        return None
    suffix = script[offset + closing.end() :]
    if not re.fullmatch(r"\s*;?\s*", suffix) and not _bootstrap_suffix(suffix):
        return None
    return value


def viewer_confirmed(body: bytes) -> bool:
    """Verify one literal viewer config, including its tightly scoped anonymous catch fallback."""
    root = etree.HTML(body, parser=etree.HTMLParser(no_network=True))
    configs = []
    for script in root.xpath("//script[not(@src)]/text()") if root is not None else []:
        # Member overwrites outside the fixed pair cannot silently alter its identity.
        if re.search(
            r"(?:window\s*\[\s*['\"]\$CONFIG['\"]\s*\]|(?:window\.)?\$CONFIG)\s*"
            r"(?:\.\s*[A-Za-z_$][A-Za-z0-9_$]*\s*|\[\s*['\"][^'\"]+['\"]\s*\]\s*)+"
            r"(?:[+*/%&|^-]?=(?!=)|\+\+|--)",
            script,
        ):
            return False
        # Count every assignment, including non-JSON/bracket-form overwrites.
        assignments = list(
            re.finditer(r"(?<![\w$])(?:window\.)?\$CONFIG\s*(?:[\"']\s*\])?\s*=", script)
        )
        if not assignments:
            continue
        if len(assignments) == 2:
            # The real root page initializes unrelated globals before this wrapper.
            # Only a statement-boundary try immediately containing the FIRST assignment
            # may pair with its catch; _script_viewer validates the entire remaining pair.
            opening = re.search(r"(?:^|;)\s*(try\s*\{\s*)$", script[: assignments[0].start()])
            if opening is None or not re.fullmatch(
                r"window\.\$CONFIG\s*=", assignments[0].group(0)
            ):
                return False
            if "$CONFIG" in script[: opening.start(1)]:
                return False
            script = script[opening.start(1) :]
        elif len(assignments) != 1:
            return False
        try:
            value = _script_viewer(script)
        except (ValueError, RecursionError):
            return False
        if value is None:
            return False
        configs.append(value)
    return len(configs) == 1


class WeiboClient:
    """One collection owns its client. No redirects, ambient proxies or arbitrary URLs."""

    def __init__(
        self,
        cookies: list[dict[str, Any]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=transport,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://weibo.com/",
                "Accept-Encoding": "identity",
            },
        )
        for cookie in cookies:
            self._client.cookies.set(
                cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"]
            )
        self.requests = 0
        self._started = time.monotonic()

    async def __aenter__(self) -> WeiboClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self._client.aclose()

    async def _read(self, path: str, params: dict[str, str] | None = None) -> bytes:
        global _LAST_REQUEST
        if path not in {
            "/",
            "/ajax/statuses/mymblog",
            "/ajax/statuses/longtext",
            "/ajax/statuses/buildComments",
        }:
            raise WeiboCollectError("schema_changed")
        scope = _LOGIN_LOG_SCOPE.set(True)
        try:
            for attempt in range(_RETRIES + 1):
                if self.requests >= _MAX_REQUESTS or time.monotonic() - self._started >= 180:
                    raise RequestBudgetReached
                retry_code = "network"
                try:
                    # The lock spans the full response; collectors cannot burst concurrently.
                    async with _GATE:
                        delay = _LAST_REQUEST + _MIN_INTERVAL - time.monotonic()
                        await asyncio.sleep(max(0.0, delay))
                        self.requests += 1
                        _LAST_REQUEST = time.monotonic()
                        async with self._client.stream(
                            "GET", "https://weibo.com" + path, params=params
                        ) as response:
                            status = response.status_code
                            if path == "/ajax/statuses/buildComments" and status in {404, 410}:
                                raise WeiboCollectError("inaccessible")
                            if status in {301, 302, 303, 307, 308, 401}:
                                raise WeiboCollectError("authentication_required")
                            if status in {403, 418}:
                                raise WeiboCollectError("risk_control")
                            if status == 429 or 500 <= status < 600:
                                retry_code = "rate_limited" if status == 429 else "network"
                            elif status != 200:
                                raise WeiboCollectError("schema_changed")
                            else:
                                # Do not decode an unbounded compressed response in httpx.
                                encoding = response.headers.get("content-encoding", "identity")
                                if encoding.lower() not in {"identity", ""}:
                                    raise WeiboCollectError("schema_changed")
                                limit = min(_MAX_BODY, 1024 * 1024) if path == "/" else _MAX_BODY
                                content_length = response.headers.get("content-length", "")
                                if content_length.isdigit() and int(content_length) > limit:
                                    raise WeiboCollectError("response_too_large")
                                body = bytearray()
                                async for part in response.aiter_bytes(chunk_size=64 * 1024):
                                    if len(body) + len(part) > limit:
                                        raise WeiboCollectError("response_too_large")
                                    body.extend(part)
                                return bytes(body)
                except httpx.TransportError:
                    retry_code = "network"
                if attempt == _RETRIES:
                    raise WeiboCollectError(retry_code) from None
                await asyncio.sleep(0.5 * (2**attempt))
        finally:
            _LOGIN_LOG_SCOPE.reset(scope)
        raise WeiboCollectError("network")

    async def verify_identity(self) -> None:
        if not viewer_confirmed(await self._read("/")):
            raise WeiboCollectError("authentication_required")

    async def _envelope(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        body = await self._read(path, params)
        if body.lstrip().startswith(b"<"):
            raise WeiboCollectError("authentication_required")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise WeiboCollectError("schema_changed") from None
        if not isinstance(result, dict):
            raise WeiboCollectError("schema_changed")
        if type(result.get("ok")) is not int or result["ok"] != 1:
            code = str(result.get("errno", result.get("error_code", "")))
            if code in {"100001", "100002", "21301", "21314", "21315", "21327", "21332"}:
                raise WeiboCollectError("authentication_required")
            raise WeiboCollectError("risk_control")
        return result

    async def json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        result = await self._envelope(path, params)
        data = result.get("data")
        if not isinstance(data, dict):
            raise WeiboCollectError("schema_changed")
        return data

    async def comments(
        self,
        post_id: str,
        uid: str,
        *,
        root_id: str | None = None,
        root_author_id: str | None = None,
        max_id: str = "0",
    ) -> dict[str, Any]:
        """Real buildComments envelope has an array; it is not timeline data-dict."""
        for value in (post_id, uid, root_id, root_author_id):
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", value)
            ):
                raise WeiboCollectError("invalid_settings")
        if (root_id is None) != (root_author_id is None):
            raise WeiboCollectError("invalid_settings")
        if not isinstance(max_id, str) or not re.fullmatch(r"[0-9]{1,32}", max_id):
            raise WeiboCollectError("invalid_checkpoint")
        params = {
            "id": root_id or post_id,
            "uid": root_author_id or uid,
            "is_reload": "1",
            "is_show_bulletin": "2",
            "is_mix": "1" if root_id else "0",
            "count": "20",
            "fetch_level": "1" if root_id else "0",
            "locale": "zh-CN",
        }
        if max_id != "0":
            params["max_id"] = max_id
        result = await self._envelope("/ajax/statuses/buildComments", params)
        data, cursor, total = result.get("data"), result.get("max_id"), result.get("total_number")
        if (
            not isinstance(data, list)
            or len(data) > 200
            or any(not isinstance(row, dict) for row in data)
        ):
            raise WeiboCollectError("schema_changed")
        if type(cursor) not in (int, str) or not re.fullmatch(r"[0-9]{1,32}", str(cursor)):
            raise WeiboCollectError("schema_changed")
        if type(total) is not int or not 0 <= total <= 10**12:
            raise WeiboCollectError("schema_changed")
        root_comment = result.get("rootComment") or None
        if root_comment is not None and not isinstance(root_comment, dict):
            raise WeiboCollectError("schema_changed")
        # Do not expose tip_msg/headers/profile responses to queue or status callers.
        return {
            "data": data,
            "max_id": str(cursor),
            "total_number": total,
            "rootComment": root_comment,
        }
