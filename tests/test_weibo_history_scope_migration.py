"""target_self 断点迁移证明：零讨论空档才允许换签名，正文断点与 T0 一概不动。"""

from __future__ import annotations

import json

import pytest

from jianshanagent.weibo_client import WeiboCollectError
from jianshanagent.weibo_history_queue import HistoryQueue

UID = "5896401674"
LEGACY = "legacy-all-public-v1"
NARROW = "target-self-v2"


def build_legacy(
    path,
    *,
    nodes: int = 0,
    progressed_comments: int = 0,
    pristine_comments: int = 2,
    pending=None,
):
    """Open a legacy all_public queue with a post breakpoint and discussion jobs."""
    queue = HistoryQueue(path, UID, LEGACY, discussion_scope="all_public")
    with queue.transaction():
        queue.db.execute(
            "INSERT INTO posts(id,url,published,row_json) VALUES('111','u','2026-01-01','{}')"
        )
        queue.db.execute(
            "UPDATE jobs SET checked=?,page=?,cursor=?,page_done=? WHERE kind='posts'",
            (123.0, 32, "5", json.dumps(["111"])),
        )
        for i in range(pristine_comments):
            queue.db.execute(
                "INSERT INTO jobs(kind,key,post_id) VALUES('comments',?,?)",
                (f"{100 + i}", f"{100 + i}"),
            )
        for i in range(progressed_comments):
            queue.db.execute(
                "INSERT INTO jobs(kind,key,post_id,checked) VALUES('comments',?,?,1.0)",
                (f"{200 + i}", f"{200 + i}"),
            )
        for i in range(nodes):
            queue.db.execute(
                "INSERT INTO nodes(post_id,id,root_id,packet,node_json)"
                " VALUES('111',?,?,?,'{}')",
                (f"{300 + i}", f"{300 + i}", "p1"),
            )
        if pending is not None:
            queue.db.execute(
                "INSERT INTO pending(singleton,value) VALUES(1,?)", (json.dumps(pending),)
            )
        queue.db.execute("INSERT INTO issues(kind,key,code) VALUES('comments','100','x')")
        queue.db.execute("INSERT INTO cursors(kind,key,cursor) VALUES('comments','100','9')")
        queue.db.execute(
            "INSERT INTO counter_reports(kind,key,reported_total,basis)"
            " VALUES('comments','100',8,'unfiltered_reported')"
        )
    t0 = {key: queue.get(key) for key in ("snapshot_at", "history_started_at", "history_phase")}
    row = queue.db.execute("SELECT * FROM jobs WHERE kind='posts'").fetchone()
    posts_job = {key: row[key] for key in row.keys()}
    queue.close()
    return t0, posts_job


def reopen(path, *, legacy_signature=LEGACY):
    return HistoryQueue(
        path,
        UID,
        NARROW,
        discussion_scope="target_self",
        legacy_signature=legacy_signature,
    )


def test_migration_keeps_posts_breakpoint_and_t0(tmp_path):
    path = tmp_path / "queue.sqlite3"
    t0, posts_job = build_legacy(path)
    queue = reopen(path)
    try:
        assert queue.get("discussion_scope") == "target_self"
        assert queue.get("signature") == NARROW
        assert {key: queue.get(key) for key in t0} == t0
        row = queue.db.execute("SELECT * FROM jobs WHERE kind='posts'").fetchone()
        assert {key: row[key] for key in row.keys()} == posts_job
        rows = queue.db.execute(
            "SELECT key,expected,root_seen FROM jobs WHERE kind='comments' ORDER BY key"
        ).fetchall()
        assert [{key: row[key] for key in row.keys()} for row in rows] == [
            {"key": "100", "expected": None, "root_seen": 0},
            {"key": "101", "expected": None, "root_seen": 0},
        ]
        for table in ("issues", "cursors", "counter_reports"):
            assert queue.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None
    finally:
        queue.close()


def test_migration_after_completion_is_noop_and_rejects_new_signature(tmp_path):
    path = tmp_path / "queue.sqlite3"
    build_legacy(path)
    first = reopen(path)
    first.close()
    again = reopen(path)
    again.close()
    with pytest.raises(WeiboCollectError) as exc:
        HistoryQueue(path, UID, "third-signature", discussion_scope="target_self")
    assert exc.value.code == "invalid_checkpoint"


@pytest.mark.parametrize(
    "variant",
    ["nodes", "progressed_comments", "replies_job", "bad_legacy", "pending_comments"],
)
def test_migration_refuses_when_discussion_state_exists(tmp_path, variant):
    path = tmp_path / "queue.sqlite3"
    pending = (
        {"job": {"kind": "comments", "key": "100"}, "logical_packets": []}
        if variant == "pending_comments"
        else None
    )
    build_legacy(
        path,
        nodes=1 if variant == "nodes" else 0,
        progressed_comments=1 if variant == "progressed_comments" else 0,
        pending=pending,
    )
    if variant == "replies_job":
        queue = HistoryQueue(path, UID, LEGACY, discussion_scope="all_public")
        with queue.transaction():
            queue.db.execute("INSERT INTO jobs(kind,key) VALUES('replies','100')")
        queue.close()
    legacy = "wrong-signature" if variant == "bad_legacy" else LEGACY
    with pytest.raises(WeiboCollectError) as exc:
        reopen(path, legacy_signature=legacy)
    assert exc.value.code == "scope_migration_blocked"
