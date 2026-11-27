"""受控微博时间线读取；仅返回离线适配器标准行，不写 checkpoint 或资料库。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from lxml import etree  # type: ignore[import-untyped]

from jianshanagent.weibo_auth import WeiboCredentialStore
from jianshanagent.weibo_client import RequestBudgetReached, WeiboClient
from jianshanagent.weibo_client import WeiboCollectError as WeiboCollectError
from jianshanagent.weibo_collector_config import parse_weibo_collector

_CST = timezone(timedelta(hours=8))
_MAX_PAGES = 40
_MAX_SECONDS = 180.0
_MAX_PAGE_POSTS = 200
_MAX_CURSOR_CHARS = 512
# Leave headroom below SourceImportService's 10 MiB file / 512 KiB line limits.
_MAX_BATCH_BYTES = 9 * 1024 * 1024
_MAX_ROW_BYTES = 500 * 1024
_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9]{1,32}\Z")


@dataclass
class WeiboFetchBatch:
    rows: list[dict[str, Any]]
    checkpoint: dict[str, Any]
    complete: bool
    fetched: int
    skipped: int
    pages: int
    limited: bool


def serialize_weibo_row(row: dict[str, Any]) -> bytes:
    """The worker must use this exact UTF-8 JSONL representation (or equivalent)."""
    try:
        line = (
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WeiboCollectError("schema_changed") from None
    if len(line) > _MAX_ROW_BYTES:
        raise WeiboCollectError("row_too_large")
    return line


def _now() -> datetime:
    return datetime.now(UTC).astimezone(_CST)


def _id(value: Any) -> str:
    if type(value) not in (str, int) or not _ID.fullmatch(str(value)):
        raise WeiboCollectError("schema_changed")
    return str(value)


def _date(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 80:
        raise WeiboCollectError("schema_changed")
    try:
        # created_at is the source of publication date, never status text or scrape time.
        parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        raise WeiboCollectError("schema_changed") from None
    return parsed.astimezone(_CST)


def _string(value: Any, maximum: int = 100_000) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise WeiboCollectError("schema_changed")
    return value.strip()


def _public_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        return ""
    if "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return ""
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return ""
        _ = url.port
    except ValueError:
        return ""
    return value


def _html_text(value: Any) -> str:
    text = _string(value)
    root = etree.HTML(text, parser=etree.HTMLParser(no_network=True))
    if root is None:
        return ""
    for node in root.xpath("//script|//style"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    for node in root.xpath("//a[@href]"):
        href = _public_url(node.get("href"))
        if href and href not in "".join(node.itertext()):
            node.tail = f" ({href})" + (node.tail or "")
    for node in root.xpath("//br|//p|//div"):
        node.tail = "\n" + (node.tail or "")
    return _string("".join(root.itertext()))


def _native_ad(post: dict[str, Any]) -> bool:
    """Only the observed native boolean can exempt a timeline advertising slot."""
    return post.get("isAd") is True


def _ad_slot_id(post: dict[str, Any]) -> str | None:
    # Advertising slots need not carry a normal post's author/date/alias fields.
    # A valid native ID is used only for the proposed page_done skip counter.
    value = post.get("idstr", post.get("id"))
    return str(value) if type(value) in (str, int) and _ID.fullmatch(str(value)) else None


def _platform_card(post: dict[str, Any], uid: str) -> bool:
    """Observed follow-recommendation card: a platform-inserted promo row carrying
    a foreign user plus its own follow button. Only the observed marker pair with
    a non-target author exempts it; a genuine foreign row without the markers
    still hard-stops, and a target-authored row is never dropped."""
    if "followBtnCode" not in post or "buttons" not in post:
        return False
    user = post.get("user")
    author = str(user.get("idstr") or user.get("id") or "") if isinstance(user, dict) else ""
    return author != uid


def _public(post: dict[str, Any]) -> bool:
    if _native_ad(post):
        return False
    visible = post.get("visible")
    if not isinstance(visible, dict) or type(visible.get("type")) is not int:
        return False
    if visible["type"] != 0:
        return False
    flags = (
        "is_ad",
        "ad_state",
        "ad_info",
        "promotion",
        "promotion_info",
        "is_paid",
        "paid",
        "isPaid",
        "paid_type",
        "pay_type",
        "is_pay",
        "is_advertisement",
        "mblog_vip_type",
        "is_mblog_vip",
    )
    return not any(post.get(key) for key in flags)


def _identity(post: dict[str, Any]) -> tuple[str, str, str, str]:
    post_id = _id(post.get("idstr", post.get("id")))
    if "id" in post and _id(post["id"]) != post_id:
        raise WeiboCollectError("schema_changed")
    user = post.get("user")
    if not isinstance(user, dict):
        raise WeiboCollectError("schema_changed")
    author_id = _id(user.get("idstr", user.get("id")))
    if "id" in user and _id(user["id"]) != author_id:
        raise WeiboCollectError("schema_changed")
    alias = post.get("mblogid")
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
        raise WeiboCollectError("schema_changed")
    return post_id, author_id, _string(user.get("screen_name"), 200), alias


async def _row(post: dict[str, Any], client: WeiboClient) -> dict[str, Any]:
    post_id, author_id, author_name, alias = _identity(post)
    long = post.get("isLongText", False)
    if type(long) not in (bool, int) or long not in (0, 1):
        raise WeiboCollectError("schema_changed")
    content = post
    incomplete = False
    if long:
        try:
            content = await client.json("/ajax/statuses/longtext", {"id": alias})
        except WeiboCollectError as error:
            if error.code in {"schema_changed", "network"}:
                raise WeiboCollectError("detail_unavailable") from None
            raise
        if isinstance(content.get("longTextContent_raw"), str):
            text = _string(content["longTextContent_raw"])
        elif isinstance(content.get("longTextContent"), str):
            text = (
                _string(content["longTextContent"])
                if content.get("isMarkdown") is True
                else _html_text(content["longTextContent"])
            )
        else:
            text = ""
        if not text:
            # An emptied or hidden long-text detail keeps the public truncated
            # list text and records the gap; nothing collectable still stops.
            text = (
                _string(post["text_raw"])
                if isinstance(post.get("text_raw"), str)
                else _html_text(post.get("text"))
            )
            if not text:
                raise WeiboCollectError("detail_unavailable")
            incomplete = True
    elif isinstance(content.get("text_raw"), str):
        text = _string(content["text_raw"])
    else:
        text = _html_text(content.get("text"))
    media: list[str] = []
    pic_infos = post.get("pic_infos", {})
    if not isinstance(pic_infos, dict) or len(pic_infos) > 100:
        raise WeiboCollectError("schema_changed")
    for pic in pic_infos.values():
        if not isinstance(pic, dict):
            raise WeiboCollectError("schema_changed")
        original = pic.get("original", pic.get("large", {}))
        if isinstance(original, dict) and (url := _public_url(original.get("url"))):
            media.append(url)
    if media:
        text += "\n\n公开图片：\n" + "\n".join(dict.fromkeys(media))
    if not text:
        raise WeiboCollectError("schema_changed")
    result: dict[str, Any] = {
        "version": 1,
        "id": post_id,
        "text": _string(text),
        **({"text_incomplete": True} if incomplete else {}),
        "url": f"https://weibo.com/{author_id}/{alias}",
        "published_at": _date(post.get("created_at")).isoformat(),
        "author_id": author_id,
        "author_name": author_name,
    }
    # Deliberately omit collected_at: identical upstream content remains byte-stable.
    repost = post.get("retweeted_status")
    if repost is not None:
        if not isinstance(repost, dict) or not _public(repost):
            raise WeiboCollectError("schema_changed")
        try:
            reference = await _row_reference(repost, client)
        except WeiboCollectError as error:
            # Deleted, invisible or unfetchable reposted original: keep the
            # outer post and drop the shallow reference (skip record).
            if error.code not in {"schema_changed", "detail_unavailable"}:
                raise
        else:
            if reference["id"] == post_id:
                raise WeiboCollectError("schema_changed")
            result["repost"] = reference
    return result


async def _row_reference(post: dict[str, Any], client: WeiboClient) -> dict[str, Any]:
    # Never recursively import or elevate a third-party author into a source document.
    shallow = dict(post)
    shallow.pop("retweeted_status", None)
    result = await _row(shallow, client)
    return {k: v for k, v in result.items() if k not in {"version"}}


def _valid_cursor(value: Any) -> bool:
    # An upstream cursor is opaque. It is only a bounded query value, never a URL/path.
    return (
        isinstance(value, str)
        and len(value) <= _MAX_CURSOR_CHARS
        and all(32 <= ord(char) <= 126 for char in value)
    )


def _checkpoint(settings: dict[str, Any], checkpoint: Any) -> dict[str, Any]:
    uid = settings["uid"]
    if checkpoint is not None:
        try:
            if not isinstance(checkpoint, dict) or set(checkpoint) != {
                "version",
                "uid",
                "window_start",
                "window_end",
                "initial_complete",
                "complete",
                "next_page",
                "cursor",
                "page_done",
                "previous_ids",
            }:
                raise ValueError
            if checkpoint["version"] != 1 or type(checkpoint["version"]) is not int:
                raise ValueError
            if checkpoint["uid"] != uid:
                raise ValueError
            for key in ("complete", "initial_complete"):
                if type(checkpoint[key]) is not bool:
                    raise ValueError
            page = checkpoint["next_page"]
            if type(page) is not int or not 1 <= page <= 100_000:
                raise ValueError
            cursor = checkpoint["cursor"]
            if not _valid_cursor(cursor):
                raise ValueError
            for key in ("page_done", "previous_ids"):
                ids = checkpoint[key]
                if not isinstance(ids, list) or len(ids) > _MAX_PAGE_POSTS:
                    raise ValueError
                for item in ids:
                    if not isinstance(item, str) or not _ID.fullmatch(item):
                        raise ValueError
            start = datetime.fromisoformat(checkpoint["window_start"])
            end = datetime.fromisoformat(checkpoint["window_end"])
            if start.tzinfo is None or end.tzinfo is None or not start < end:
                raise ValueError
            if end - start > timedelta(days=365):
                raise ValueError
            if checkpoint["complete"] and not checkpoint["initial_complete"]:
                raise ValueError
            if not checkpoint["complete"]:
                return {**checkpoint, "page_done": list(checkpoint["page_done"])}
        except (ValueError, TypeError, KeyError):
            raise WeiboCollectError("invalid_checkpoint") from None
    end = _now()
    initial_complete = bool(checkpoint and checkpoint["initial_complete"])
    if initial_complete:
        # Anchor to the last COMMITTED window, not now: downtime must not create gaps.
        successful_end = datetime.fromisoformat(checkpoint["window_end"])
        if successful_end > end:
            raise WeiboCollectError("invalid_checkpoint")
        start = successful_end - timedelta(days=settings["overlap_days"])
        # A long outage is caught up by consecutive overlapping bounded windows.
        end = min(end, start + timedelta(days=365))
    else:
        start = end - timedelta(days=settings["initial_days"])
    return {
        "version": 1,
        "uid": uid,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "initial_complete": initial_complete,
        "complete": False,
        "next_page": 1,
        "cursor": "",
        "page_done": [],
        "previous_ids": [],
    }


async def fetch_weibo(
    settings: Mapping[str, Any],
    credential_store: WeiboCredentialStore,
    *,
    checkpoint: dict[str, Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> WeiboFetchBatch:
    """Return proposed checkpoint; ONLY the caller may persist it after successful import."""
    try:
        config = parse_weibo_collector(dict(settings))
    except (ValueError, TypeError):
        raise WeiboCollectError("invalid_settings") from None
    if config is None or config.get("mode") == "full_then_incremental":
        # Full history requires the durable SQLite coordinator; never silently run
        # the legacy bounded-date path for a full-history configuration.
        raise WeiboCollectError("invalid_settings")
    target_end = _now()
    proposed = _checkpoint(config, checkpoint)
    try:
        cookies = credential_store.read_for_collection()
    except (ValueError, OSError):
        raise WeiboCollectError("authentication_required") from None
    rows: list[dict[str, Any]] = []
    fetched = skipped = pages = 0
    batch_bytes = 0
    seen: set[str] = set()
    complete = False
    start = datetime.fromisoformat(proposed["window_start"])
    end = datetime.fromisoformat(proposed["window_end"])
    async with WeiboClient(cookies) as client:
        try:
            async with asyncio.timeout(_MAX_SECONDS):
                await client.verify_identity()
                while pages < _MAX_PAGES:
                    params = {
                        "uid": config["uid"],
                        "page": str(proposed["next_page"]),
                        "feature": "0",
                    }
                    if proposed["cursor"]:
                        params["since_id"] = proposed["cursor"]
                    data = await client.json("/ajax/statuses/mymblog", params)
                    posts = data.get("list")
                    if not isinstance(posts, list) or len(posts) > _MAX_PAGE_POSTS:
                        raise WeiboCollectError("schema_changed")
                    pages += 1
                    if not posts:
                        # A normal terminal page may omit its now-unused cursor entirely.
                        complete = True
                        break
                    cursor = data.get("since_id")
                    if type(cursor) not in (str, int) or not _valid_cursor(str(cursor)):
                        raise WeiboCollectError("schema_changed")
                    next_cursor = str(cursor)
                    # Head insertions may shift a resumed page. Only retain this page's
                    # already committed IDs; moved IDs can safely be imported idempotently.
                    current_ids = {
                        str(item.get("idstr", item.get("id")))
                        for item in posts
                        if isinstance(item, dict)
                    }
                    done = set(proposed["page_done"]) & current_ids
                    proposed["page_done"] = sorted(done)
                    page_ids: list[str] = []
                    ordinary_dates: list[datetime] = []
                    for post in posts:
                        if not isinstance(post, dict):
                            raise WeiboCollectError("schema_changed")
                        if _native_ad(post) or _platform_card(post, config["uid"]):
                            ad_id = _ad_slot_id(post)
                            if ad_id is None or ad_id not in done:
                                skipped += 1
                            if ad_id is not None:
                                done.add(ad_id)
                                proposed["page_done"] = sorted(done)
                            continue
                        post_id, author, _name, _alias = _identity(post)
                        if author != config["uid"]:
                            raise WeiboCollectError("author_mismatch")
                        published = _date(post.get("created_at"))
                        page_ids.append(post_id)
                        if not post.get("isTop") and not post.get("is_top"):
                            ordinary_dates.append(published)
                        if post_id in done or post_id in seen:
                            skipped += 1
                            continue
                        fetched += 1
                        repost = post.get("retweeted_status")
                        allowed = (
                            start <= published <= end
                            and _public(post)
                            and (config["include_reposts"] or repost is None)
                            and (repost is None or isinstance(repost, dict) and _public(repost))
                        )
                        if allowed:
                            row = await _row(post, client)
                            row_bytes = len(serialize_weibo_row(row))
                            if row_bytes > _MAX_BATCH_BYTES:
                                raise WeiboCollectError("row_too_large")
                            if batch_bytes + row_bytes > _MAX_BATCH_BYTES:
                                # Do not mark this post done: the next batch must retry it.
                                return WeiboFetchBatch(
                                    rows, proposed, False, fetched, skipped, pages, True
                                )
                            rows.append(row)
                            batch_bytes += row_bytes
                        else:
                            skipped += 1
                        seen.add(post_id)
                        done.add(post_id)
                        proposed["page_done"] = sorted(done)
                        if progress is not None:
                            progress(
                                {
                                    "phase": "fetching",
                                    "pages": pages,
                                    "fetched": fetched,
                                    "accepted": len(rows),
                                    "skipped": skipped,
                                }
                            )
                        if len(rows) >= config["max_posts"]:
                            return WeiboFetchBatch(
                                rows, proposed, False, fetched, skipped, pages, True
                            )
                    if page_ids and set(page_ids) <= set(proposed["previous_ids"]):
                        raise WeiboCollectError("pagination_stalled")
                    if ordinary_dates and all(published < start for published in ordinary_dates):
                        complete = True
                        break
                    if next_cursor and next_cursor == proposed["cursor"]:
                        raise WeiboCollectError("pagination_stalled")
                    proposed.update(
                        next_page=proposed["next_page"] + 1,
                        cursor=next_cursor,
                        page_done=[],
                        previous_ids=page_ids,
                    )
        except (RequestBudgetReached, TimeoutError):
            # Current post only enters page_done after its complete detail succeeded.
            pass
    if complete:
        proposed.update(complete=True, initial_complete=True, page_done=[], previous_ids=[])
        if end < target_end:
            # The bounded historic segment succeeded, but the outage is not yet caught up.
            # Propose the next segment; it becomes resumable ONLY after these rows commit.
            proposed = _checkpoint(config, proposed)
            complete = False
    return WeiboFetchBatch(rows, proposed, complete, fetched, skipped, pages, not complete)
