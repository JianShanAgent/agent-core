"""完整历史单页采集与资源测量；队列确认由发布入口负责。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import resource
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jianshanagent.weibo_auth import WeiboCredentialStore
from jianshanagent.weibo_client import RequestBudgetReached, WeiboClient, WeiboCollectError
from jianshanagent.weibo_collect import (
    _ad_slot_id,
    _date,
    _html_text,
    _id,
    _identity,
    _native_ad,
    _public,
    _public_url,
    _row,
    _string,
    _valid_cursor,
    serialize_weibo_row,
)
from jianshanagent.weibo_history_queue import PACKET_CHARS, HistoryQueue

DISK_BUDGET = 4 * 1024**3
FREE_FLOOR = 3 * 1024**3
VECTOR_BUDGET = 512 * 1024**2
RSS_BUDGET = 256 * 1024**2
MAX_BATCH_BYTES = 9 * 1024**2


def discussion_node(value: Any, post_id: str, *, root_id: str | None = None) -> dict[str, Any]:
    """Keep public discussion facts only; never infer relationships from prose or names."""
    if not isinstance(value, dict):
        raise WeiboCollectError("schema_changed")
    node_id = _id(value.get("idstr", value.get("id")))
    if "id" in value and _id(value["id"]) != node_id:
        raise WeiboCollectError("schema_changed")
    user = value.get("user")
    if not isinstance(user, dict):
        raise WeiboCollectError("schema_changed")
    author = _id(user.get("idstr", user.get("id")))
    if "id" in user and _id(user["id"]) != author:
        raise WeiboCollectError("schema_changed")
    status = value.get("status")
    if status is not None and (
        not isinstance(status, dict) or _id(status.get("idstr", status.get("id"))) != post_id
    ):
        raise WeiboCollectError("author_mismatch")
    root = root_id or node_id
    for field in ("rootid", "root_id"):
        if value.get(field) not in (None, 0, "0", "") and _id(value[field]) != root:
            raise WeiboCollectError("author_mismatch")
    parent = value.get("reply_comment")
    parent_id = None
    if parent is not None:
        if not isinstance(parent, dict):
            raise WeiboCollectError("schema_changed")
        if parent.get("idstr", parent.get("id")) not in (None, 0, "0"):
            parent_id = _id(parent.get("idstr", parent.get("id")))
    direct = value.get("reply_to_id")
    reply_to = _id(direct) if direct not in (None, 0, "0", "") else parent_id
    # Explicit full-text fields outrank a coexisting text_raw preview. An explicit
    # truncation marker without full content must never become a successful archive.
    full_key = next(
        (
            key
            for key in ("longTextContent_raw", "text_long", "longTextContent")
            if isinstance(value.get(key), str) and value[key].strip()
        ),
        None,
    )
    truncated = any(value.get(key) for key in ("isLongText", "is_long_text", "truncated"))
    if truncated and full_key is None:
        raise WeiboCollectError("detail_unavailable")
    raw = value[full_key] if full_key else value.get("text_raw")
    if isinstance(raw, str) and len(raw) > PACKET_CHARS:
        raise WeiboCollectError("row_too_large")
    if full_key == "longTextContent" and value.get("isMarkdown") is not True:
        text = _html_text(raw)
    else:
        text = _string(raw) if isinstance(raw, str) else _html_text(value.get("text"))
    if not text:
        raise WeiboCollectError("schema_changed")
    node: dict[str, Any] = {
        "id": node_id,
        "root_id": root,
        "parent_id": parent_id,
        "reply_to_id": reply_to,
        "author_id": author,
        "author_name": _string(user.get("screen_name"), 200),
        "published_at": _date(value.get("created_at")).isoformat(),
        "text": text,
    }
    media = []
    pic = value.get("pic")
    if isinstance(pic, dict):
        for key in ("large", "original", "url"):
            candidate = pic.get(key)
            url = _public_url(candidate.get("url") if isinstance(candidate, dict) else candidate)
            if url:
                media.append(url)
    if media:
        node["media_refs"] = list(dict.fromkeys(media))[:16]
    return node


def _comment_author(value: Any) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("user"), dict):
        raise WeiboCollectError("schema_changed")
    user = value["user"]
    author = _id(user.get("idstr", user.get("id")))
    if "id" in user and _id(user["id"]) != author:
        raise WeiboCollectError("schema_changed")
    return author


def _page_comments(
    queue: HistoryQueue, job: dict[str, Any], envelope: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    items = envelope.get("data")
    if not isinstance(items, list) or len(items) > 200:
        raise WeiboCollectError("schema_changed")
    scope = settings.get("discussion_scope", "all_public")
    if scope != queue.get("discussion_scope", "all_public"):
        raise WeiboCollectError("scope_migration_blocked")
    target_only = scope == "target_self"
    nodes, roots = [], []
    skipped = 0
    reply = job["kind"] == "replies"
    if reply and target_only:
        queue.require_target_root(job, settings["uid"])
    root_seen = False
    root_comment = envelope.get("rootComment")
    if reply and root_comment:
        root_node = discussion_node(root_comment, job["post_id"], root_id=job["key"])
        if root_node["id"] != job["key"] or root_node["author_id"] != job["author_id"]:
            raise WeiboCollectError("author_mismatch")
        nodes.append(root_node)
        root_seen = True
    for item in items:
        if target_only and _comment_author(item) != settings["uid"]:
            # Do not inspect or persist the excluded user's text, name, inline tree,
            # dates, media or native ID. The response's UID alone selects the lane.
            skipped += 1
            continue
        node = discussion_node(item, job["post_id"], root_id=job["key"] if reply else None)
        if reply and node["id"] == job["key"]:
            root_seen = True
        nodes.append(node)
        if not reply and settings.get("include_replies"):
            inline = item.get("comments", [])
            if not isinstance(inline, list) or len(inline) > 200:
                raise WeiboCollectError("schema_changed")
            total = item.get("total_number")
            if isinstance(total, str) and re.fullmatch(r"[0-9]{1,13}", total):
                total = int(total)
            if total is not None and (type(total) is not int or not 0 <= total <= 10**12):
                raise WeiboCollectError("schema_changed")
            for child in inline:
                if target_only and _comment_author(child) != settings["uid"]:
                    skipped += 1
                    continue
                nodes.append(discussion_node(child, job["post_id"], root_id=node["id"]))
            # For target_self even a reported zero is not a selected-scope cursor
            # proof. Never request a root owned by anybody other than the target.
            if target_only or inline or total is None or total > 0:
                roots.append({"id": node["id"], "author_id": node["author_id"],
                              "expected": None if target_only else total})
    cursor = envelope.get("max_id", 0)
    if type(cursor) not in (str, int) or not str(cursor).isdigit() or len(str(cursor)) > 32:
        raise WeiboCollectError("schema_changed")
    terminal = str(cursor) == "0"
    if not terminal and (str(cursor) == job["cursor"] or not items):
        raise WeiboCollectError("pagination_stalled")
    expected = envelope.get("total_number")
    if expected is not None and (type(expected) is not int or not 0 <= expected <= 10**12):
        raise WeiboCollectError("schema_changed")
    return queue.stage(
        job,
        nodes=nodes,
        roots=roots,
        cursor=str(cursor),
        terminal=terminal,
        expected=None if target_only else expected,
        reported_total=expected,
        root_seen=root_seen,
        include_replies=bool(settings.get("include_replies")),
        skipped=skipped,
    )


def _post_alias(queue: HistoryQueue, post_id: str) -> str:
    """Comment requests must carry the official post URL; a lost alias is a broken checkpoint."""
    url = str(queue.post(post_id).get("url", ""))
    alias = url.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", alias):
        raise WeiboCollectError("invalid_checkpoint")
    return alias


async def _posts_page(
    queue: HistoryQueue,
    settings: dict[str, Any],
    job: dict[str, Any],
    client: WeiboClient,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    deadline = deadline if deadline is not None else time.monotonic() + 170
    params = {"uid": settings["uid"], "page": str(job["page"]), "feature": "0"}
    if job["cursor"]:
        params["since_id"] = job["cursor"]
    data = await client.json("/ajax/statuses/mymblog", params)
    posts = data.get("list")
    if not isinstance(posts, list) or len(posts) > 200:
        raise WeiboCollectError("schema_changed")
    cursor = data.get("since_id") if posts else data.get("since_id", "")
    if type(cursor) not in (str, int) or not _valid_cursor(str(cursor)):
        raise WeiboCollectError("schema_changed")
    cursor = str(cursor)
    if posts and cursor and cursor == job["cursor"]:
        raise WeiboCollectError("pagination_stalled")
    rows: list[dict[str, Any]] = []
    size = 0
    ordinary_dates = []
    snapshot = datetime.fromisoformat(queue.get("snapshot_at"))
    lower = datetime.fromisoformat(queue.get("incremental_since", queue.get("snapshot_at")))
    upper = datetime.fromisoformat(queue.get("catchup_until", queue.get("snapshot_at")))
    previous = set(json.loads(job.get("page_done", "[]")))
    current_ids: set[str] = set()
    ordinary_ids: set[str] = set()
    for item in posts:
        if not isinstance(item, dict):
            raise WeiboCollectError("schema_changed")
        identifier = (
            _ad_slot_id(item) if _native_ad(item) else _id(item.get("idstr", item.get("id")))
        )
        if identifier is not None:
            current_ids.add(identifier)
            if not _native_ad(item):
                ordinary_ids.add(identifier)
    processed = previous & current_ids
    skipped = 0
    advance = True
    limit = int(settings.get("max_posts", 200))
    status = queue.status()
    if not queue.get("measurement", {}).get("evaluated"):
        limit = min(limit, max(1, 100 - status["posts_total"]))
    for post in posts:
        if not isinstance(post, dict):
            raise WeiboCollectError("schema_changed")
        if _native_ad(post):
            ad_id = _ad_slot_id(post)
            if ad_id is None or ad_id not in processed:
                skipped += 1
            if ad_id is not None:
                processed.add(ad_id)
            continue
        native, author, _name, _alias = _identity(post)
        if author != settings["uid"]:
            raise WeiboCollectError("author_mismatch")
        published = _date(post.get("created_at"))
        if not post.get("isTop") and not post.get("is_top"):
            ordinary_dates.append(published)
        if native in processed:
            continue
        within = published <= snapshot if job["kind"] == "posts" else lower <= published <= upper
        if (
            within
            and _public(post)
            and (settings.get("include_reposts", False) or post.get("retweeted_status") is None)
        ):
            if len(rows) >= limit or time.monotonic() >= deadline:
                advance = False
                break
            try:
                async with asyncio.timeout(max(0.01, deadline - time.monotonic())):
                    row = await _row(post, client)
            except (RequestBudgetReached, TimeoutError):
                advance = False
                break
            next_size = size + len(serialize_weibo_row(row))
            if next_size > MAX_BATCH_BYTES:
                if not rows:
                    raise WeiboCollectError("row_too_large")
                advance = False
                break
            rows.append(row)
            size = next_size
        processed.add(native)
    terminal = advance and not posts
    if (
        advance
        and job["kind"] == "catchup"
        and ordinary_dates
        and all(d < lower for d in ordinary_dates)
    ):
        terminal = True
    return queue.stage(
        job,
        post_rows=rows,
        cursor=cursor if advance else job["cursor"],
        terminal=terminal,
        include_comments=bool(settings.get("include_comments")),
        include_replies=bool(settings.get("include_replies")),
        advance_page=advance,
        processed_ids=sorted(processed),
        page_identity_ids=sorted(ordinary_ids),
        skipped=skipped,
    )


async def fetch_history_page(
    queue: HistoryQueue,
    settings: dict[str, Any],
    credential_store: WeiboCredentialStore,
    *,
    client_factory: Callable[..., Any] = WeiboClient,
) -> dict[str, Any] | None:
    """A bounded candidate page, replayed byte-for-byte until publication succeeds."""
    pending = queue.pending()
    if pending is not None:
        return pending
    queue.prepare_incremental()
    job = queue.job()
    if job is None:
        return None
    if settings.get("discussion_scope", "all_public") == "target_self" and job["kind"] == "replies":
        queue.require_target_root(job, settings["uid"])
    try:
        cookies = credential_store.read_for_collection()
    except (ValueError, OSError):
        raise WeiboCollectError("authentication_required") from None
    started = time.monotonic()
    async with client_factory(cookies) as client:
        async with asyncio.timeout(180):
            await client.verify_identity()
            if job["kind"] in {"posts", "catchup"}:
                return await _posts_page(queue, settings, job, client, deadline=started + 170)
            try:
                envelope = await client.comments(
                    job["post_id"],
                    settings["uid"],
                    max_id=job["cursor"] or "0",
                    root_id=job["key"] if job["kind"] == "replies" else None,
                    root_author_id=job["author_id"] if job["kind"] == "replies" else None,
                    post_alias=_post_alias(queue, job["post_id"]),
                )
            except WeiboCollectError as exc:
                if exc.code == "inaccessible":
                    queue.inaccessible(job)
                    return None
                raise
            return _page_comments(queue, job, envelope, settings)


def tree_usage(paths: list[Path]) -> tuple[int, int]:
    """Walk metadata only, never read a source archive/vector into process memory."""
    total = vectors = 0
    for root in paths:
        if not root.exists():
            continue
        for directory, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise WeiboCollectError("invalid_checkpoint")
                size = path.stat().st_size
                total += size
                if "vector" in name or path.suffix in {".f32", ".npy"}:
                    vectors += size
    return total, vectors


def measure_resources(
    queue: HistoryQueue, paths: list[Path], *, pg_bytes: int = 0
) -> dict[str, Any]:
    """Fail closed at the shared budget, including baseline and bounded growth forecast."""
    disk, vectors = tree_usage(paths)
    free = shutil.disk_usage(queue.path.parent).free
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = int(rss if os.uname().sysname == "Darwin" else rss * 1024)
    with queue.transaction():
        if queue.get("resource_baseline") is None:
            queue.set("resource_baseline", disk)
            queue.set("rss_baseline", rss_bytes)
        growth = max(0, disk - queue.get("resource_baseline")) + max(0, pg_bytes)
        status = queue.status()
        previous = queue.get("measurement", {})
        nodes = status["comments_total"] + status["replies_total"]
        reached = status["posts_total"] >= 100 or nodes >= 10_000
        elapsed = max(0.0, time.time() - queue.get("history_started_at"))
        measurement = {
            "posts": status["posts_total"],
            "discussion_nodes": nodes,
            "disk_bytes": disk,
            "pg_bytes": max(0, pg_bytes),
            "free_bytes": free,
            "peak_rss_bytes": rss_bytes,
            "rss_growth_bytes": max(0, rss_bytes - queue.get("rss_baseline")),
            "elapsed_seconds": round(elapsed, 3),
            "evaluated": reached,
            "sample_disk_bytes": previous.get("sample_disk_bytes", growth) if reached else growth,
        }
        queue.set("measurement", measurement)
        reason = None
        if (
            growth + 2 * MAX_BATCH_BYTES >= DISK_BUDGET
            or free < FREE_FLOOR + 2 * MAX_BATCH_BYTES
            or vectors >= VECTOR_BUDGET
        ):
            reason = "resource_limit"
        if measurement["rss_growth_bytes"] > RSS_BUDGET:
            reason = "resource_limit"
        # Reserve one full pending+rollback batch and the measured index/vector growth
        # for every already-discovered pending post. No guessed total/post target.
        if not nodes and queue.get("history_phase") == "comments":
            queue.set("discussion_resource_baseline", growth)
        if reached and nodes:
            comment_growth = max(0, growth - queue.get("discussion_resource_baseline", growth))
            per_post = comment_growth / max(1, status["posts_total"] - status["pending_comments"])
            forecast = growth + int(per_post * status["pending_comments"]) + 2 * MAX_BATCH_BYTES
            measurement["projected_known_bytes"] = forecast
            if forecast > DISK_BUDGET or forecast - growth > free - FREE_FLOOR:
                reason = "resource_limit"
        queue.set("measurement", measurement)
        if reason:
            queue.set("block_reason", reason)
            queue.set("paused", True)
    return {
        "resource_bytes": growth,
        "vector_bytes": vectors,
        "measurement": measurement,
        "blocked": reason is not None,
    }
