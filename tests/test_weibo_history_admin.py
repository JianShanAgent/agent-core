"""Real aiohttp handlers for atomic linked-discussion configuration."""

from __future__ import annotations

import pytest

from test_sources_admin import LocalEnv
from test_sources_admin import local as local

pytestmark = pytest.mark.asyncio

FULL = {
    "uid": "5896401674",
    "enabled": False,
    "mode": "full_then_incremental",
    "include_comments": True,
    "include_replies": True,
    "discussion_source_id": "official_comments",
}


async def test_create_full_parent_and_community_partition_in_one_save(local: LocalEnv):
    await local.send(
        "POST",
        "/api/sources",
        {
            "id": "official",
            "platform": "weibo",
            "label": "官方微博",
            "role": "official",
            "collector": FULL,
        },
        201,
    )
    result = await local.send("GET", "/api/sources")
    by_id = {source["id"]: source for source in result["sources"]}
    root, child = by_id["official"], by_id["official_comments"]
    assert root["role"] == "official" and root["storage"] == "stream_v2"
    assert root["collector"]["include_replies"] is True
    assert child["discussion_of"] == "official" and child["role"] == "community"
    assert child["collector"] is None and child["storage"] == "stream_v2"
    assert child["collection"]["state"] != "not_configured"
    assert child["collection"]["parent_source_id"] == "official"
    await local.send("POST", "/api/sources/official_comments/collect", {}, 409)


async def test_collision_never_overwrites_existing_namespace(local: LocalEnv):
    await local.send(
        "POST",
        "/api/sources",
        {
            "id": "official_comments",
            "platform": "weibo",
            "role": "official",
            "label": "已有来源",
        },
        201,
    )
    await local.send(
        "POST",
        "/api/sources",
        {
            "id": "official",
            "platform": "weibo",
            "collector": FULL,
        },
        409,
    )
    result = await local.send("GET", "/api/sources")
    sources = {source["id"]: source for source in result["sources"]}
    assert "official" not in sources
    assert sources["official_comments"]["label"] == "已有来源"
    assert sources["official_comments"]["discussion_of"] is None


async def test_enable_full_history_on_existing_source_preserves_role_and_id(local: LocalEnv):
    await local.send(
        "POST",
        "/api/sources",
        {
            "id": "official",
            "platform": "weibo",
            "role": "official",
            "mounted": True,
            "collector": {"uid": "5896401674"},
        },
        201,
    )
    await local.send("PUT", "/api/sources/official", {"collector": FULL})
    result = await local.send("GET", "/api/sources")
    sources = {source["id"]: source for source in result["sources"]}
    assert sources["official"]["role"] == "official"
    assert sources["official"]["mounted"] is True
    assert sources["official_comments"]["discussion_of"] == "official"
    await local.send("PUT", "/api/sources/official_comments", {"role": "official"}, 409)
    await local.send("PUT", "/api/sources/official_comments", {"discussion_of": "missing"}, 409)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "infinite"),
        ("include_comments", "yes"),
        ("include_replies", 1),
        ("discussion_source_id", "../elsewhere"),
    ],
)
async def test_bad_scope_rejected_before_any_source_is_saved(local: LocalEnv, field, value):
    await local.send(
        "POST",
        "/api/sources",
        {
            "id": "official",
            "platform": "weibo",
            "collector": {**FULL, field: value},
        },
        400,
    )
    result = await local.send("GET", "/api/sources")
    assert not any(s["id"] in {"official", "official_comments"} for s in result["sources"])
