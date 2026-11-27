"""Full-history configuration preserves legacy defaults and discussion ownership."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jianshanagent.core.config import load_config, write_source_instances
from jianshanagent.core.sources import disabled_source_ids
from jianshanagent.weibo_collector_config import parse_weibo_collector


def _collector(**extra: object) -> dict:
    return {"uid": "5896401674", **extra}


def _parent(**extra: object) -> dict:
    return {
        "id": "weibo_yys",
        "platform": "weibo",
        "label": "官方微博",
        "role": "official",
        "mounted": True,
        "storage": "stream_v2",
        "collector": _collector(
            mode="full_then_incremental",
            include_comments=True,
            include_replies=True,
            discussion_source_id="weibo_yys_comments",
        ),
        **extra,
    }


def _child(**extra: object) -> dict:
    return {
        "id": "weibo_yys_comments",
        "platform": "weibo",
        "label": "微博评论",
        "role": "community",
        "mounted": True,
        "storage": "stream_v2",
        "discussion_of": "weibo_yys",
        **extra,
    }


def _config(tmp_path: Path, rows: list[dict]):
    root = tmp_path / "config"
    root.mkdir()
    (root / "core.wiki.yaml").write_text("wikis: []\n", encoding="utf-8")
    (root / "core.sources.yaml").write_text(yaml.safe_dump({"sources": rows}), encoding="utf-8")
    return load_config(root, overrides_dir=tmp_path / "overrides")


def test_legacy_collector_serialization_unchanged():
    value = parse_weibo_collector(_collector())
    assert value == {
        "uid": "5896401674",
        "enabled": False,
        "include_reposts": True,
        "initial_days": 30,
        "overlap_days": 7,
        "max_posts": 200,
        "interval_seconds": 1800,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "all"},
        {"mode": True},
        {"discussion_scope": "other_users"},
        {"discussion_scope": True},
        {"discussion_scope": ["target_self"]},
        {"include_comments": "true"},
        {"include_replies": 1},
        {"include_replies": True},
        {"mode": "full_then_incremental", "include_comments": True},
        {"mode": "recent", "include_comments": True, "discussion_source_id": "child"},
        {"discussion_source_id": "../child"},
        {"discussion_source_id": "a" * 33},
        {"comment_url": "https://example.invalid"},
        {"Cookie": "forbidden"},
    ],
)
def test_collector_scope_strict(extra):
    with pytest.raises(ValueError):
        parse_weibo_collector(_collector(**extra))


def test_full_scope_roundtrip_and_community_binding(tmp_path):
    config = _config(tmp_path, [_parent(), _child()])
    assert config.source("weibo_yys").role == "official"
    child = config.source("weibo_yys_comments")
    assert child.role == "community" and child.raw["discussion_of"] == "weibo_yys"
    assert "collector" not in child.raw
    write_source_instances(config, [_parent(), _child()])
    restored = load_config(config.root, overrides_dir=config.overrides_dir)
    assert restored.source("weibo_yys").raw["collector"]["include_replies"] is True


@pytest.mark.parametrize(
    "rows",
    [
        [_parent()],
        [_child()],
        [_parent(storage="json"), _child()],
        [_parent(), _child(role="official")],
        [_parent(), _child(collector=_collector())],
        [_parent(), _child(discussion_of="missing")],
        [_parent(), _child(discussion_of="weibo_yys_comments")],
        [_parent(), _child(platform="bilibili")],
        [_parent(), _child(), _child(id="second")],
    ],
)
def test_invalid_source_relationships_rejected(tmp_path, rows):
    with pytest.raises(ValueError):
        _config(tmp_path, rows)


def test_parent_unmounted_hides_discussion_without_removing_data(tmp_path):
    config = _config(tmp_path, [_parent(mounted=False), _child()])
    assert disabled_source_ids(config) == {"weibo_yys", "weibo_yys_comments"}
    assert config.source("weibo_yys_comments").raw["mounted"] is True


def test_storage_marker_cannot_be_removed(tmp_path):
    config = _config(tmp_path, [_parent(collector=_collector())])
    replacement = _parent(collector=_collector())
    replacement.pop("storage")
    with pytest.raises(ValueError, match="stream_v2"):
        write_source_instances(config, [replacement])


def test_discussion_cannot_be_reassigned_even_when_empty(tmp_path):
    other = _parent(id="other", collector=_collector())
    config = _config(tmp_path, [_parent(), _child(), other])
    root = _parent(collector=_collector())
    with pytest.raises(ValueError, match="父来源"):
        write_source_instances(config, [root, _child(discussion_of="other"), other])


def test_backend_creates_child_atomically_and_never_overwrites_collision():
    from jianshanagent.admin.routes_sources import _with_discussion_sources

    parent = _parent()
    parent.pop("storage")
    result = _with_discussion_sources([parent])
    assert len(result) == 2 and result[0]["storage"] == "stream_v2"
    assert result[1]["id"] == "weibo_yys_comments"
    assert result[1]["role"] == "community" and "collector" not in result[1]
    assert "storage" not in parent
    assert _with_discussion_sources(result) == result
    with pytest.raises(ValueError, match="占用"):
        _with_discussion_sources(
            [parent, {"id": "weibo_yys_comments", "platform": "weibo", "role": "official"}]
        )

        @pytest.mark.parametrize("scope", ["all_public", "target_self"])
        def test_explicit_discussion_scope_roundtrips_without_changing_legacy(tmp_path, scope):
            parent = _parent()
            parent["collector"]["discussion_scope"] = scope
            config = _config(tmp_path, [parent, _child()])
            assert config.source("weibo_yys").raw["collector"]["discussion_scope"] == scope
            write_source_instances(config, [parent, _child()])
            restored = load_config(config.root, overrides_dir=config.overrides_dir)
            assert restored.source("weibo_yys").raw["collector"]["discussion_scope"] == scope
            assert "discussion_scope" not in parse_weibo_collector(_collector())
