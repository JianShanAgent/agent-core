"""磁盘有界历史队列：先保存候选页，只有资料发布成功后才确认水位。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jianshanagent.weibo_client import WeiboCollectError

SCHEMA_VERSION = 2
PACKET_NODES = 16
PACKET_CHARS = 100_000


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class HistoryQueue:
    """One writer under the source-group lease; status never contains the task graph."""

    def __init__(self, path: Path, uid: str, signature: str, *, now: float | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise WeiboCollectError("invalid_checkpoint")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        self.path = path
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA cache_size=-4096")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                kind TEXT NOT NULL,key TEXT NOT NULL,post_id TEXT NOT NULL DEFAULT '',
                author_id TEXT NOT NULL DEFAULT '',cursor TEXT NOT NULL DEFAULT '',
                page INTEGER NOT NULL DEFAULT 1,done INTEGER NOT NULL DEFAULT 0,
                expected INTEGER,root_seen INTEGER NOT NULL DEFAULT 0,
                page_done TEXT NOT NULL DEFAULT '[]',
                checked REAL NOT NULL DEFAULT 0, PRIMARY KEY(kind,key));
            CREATE INDEX IF NOT EXISTS job_work ON jobs(done,checked,kind);
            CREATE INDEX IF NOT EXISTS job_counts ON jobs(kind,done);
            CREATE TABLE IF NOT EXISTS posts(
                id TEXT PRIMARY KEY,url TEXT NOT NULL,published TEXT NOT NULL,
                row_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS nodes(
                post_id TEXT NOT NULL,id TEXT NOT NULL,root_id TEXT NOT NULL,
                packet TEXT NOT NULL,node_json TEXT NOT NULL,published INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(post_id,id));
            CREATE TABLE IF NOT EXISTS packets(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,post_id TEXT NOT NULL,seed TEXT NOT NULL,
                members INTEGER NOT NULL,chars INTEGER NOT NULL,UNIQUE(post_id,seed));
            CREATE INDEX IF NOT EXISTS packet_tail ON packets(post_id,sequence DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS native_nodes ON nodes(id);
            CREATE INDEX IF NOT EXISTS packet_nodes ON nodes(post_id,packet);
            CREATE INDEX IF NOT EXISTS root_nodes ON nodes(post_id,root_id);
            CREATE TABLE IF NOT EXISTS cursors(
                kind TEXT NOT NULL,key TEXT NOT NULL,cursor TEXT NOT NULL,
                PRIMARY KEY(kind,key,cursor));
            CREATE TABLE IF NOT EXISTS pages(
                kind TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,
                PRIMARY KEY(kind,key,digest));
            CREATE TABLE IF NOT EXISTS issues(
                kind TEXT NOT NULL,key TEXT NOT NULL,code TEXT NOT NULL,
                expected INTEGER,observed INTEGER, PRIMARY KEY(kind,key));
            CREATE TABLE IF NOT EXISTS pending(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),value TEXT NOT NULL);
        """)
        if self.get("version") is None:
            stamp = time.time() if now is None else now
            with self.transaction():
                self.set("version", SCHEMA_VERSION)
                self.set("uid", uid)
                self.set("signature", signature)
                self.set("snapshot_at", datetime.fromtimestamp(stamp, UTC).isoformat())
                self.set("history_started_at", stamp)
                self.set("history_phase", "posts")
                self.set("history_complete", False)
                self.set("paused", False)
                self.db.execute("INSERT INTO jobs(kind,key) VALUES('posts','timeline')")
        if self.get("version") != SCHEMA_VERSION or self.get("uid") != uid:
            self.close()
            raise WeiboCollectError("invalid_checkpoint")
        if self.get("signature") != signature:
            self.close()
            raise WeiboCollectError("invalid_checkpoint")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> HistoryQueue:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, _dump(value)))

    def pause(self, value: bool = True, *, reason: str | None = None) -> None:
        with self.transaction():
            self.set("paused", value)
            self.set("block_reason", reason)
            if not value and self.db.execute("SELECT 1 FROM issues LIMIT 1").fetchone():
                # Manual resume rechecks unresolved evidence; it never silently waives it.
                self.db.execute(
                    "DELETE FROM cursors WHERE EXISTS(SELECT 1 FROM issues i "
                    "WHERE i.kind=cursors.kind AND i.key=cursors.key)"
                )
                self.db.execute(
                    "UPDATE jobs SET done=0,cursor='',page=1,page_done='[]' "
                    "WHERE EXISTS(SELECT 1 FROM issues i WHERE i.kind=jobs.kind AND i.key=jobs.key)"
                )
                self.db.execute(
                    "DELETE FROM pages WHERE EXISTS(SELECT 1 FROM issues i "
                    "WHERE i.kind=pages.kind AND i.key=pages.key)"
                )
                if not self.get("history_complete"):
                    self.set("history_phase", "comments")

    def pending(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT value FROM pending WHERE singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    def job(self) -> dict[str, Any] | None:
        phase = self.get("history_phase")
        kinds: tuple[str, ...]
        if phase == "posts":
            kinds = ("posts",)
        elif phase == "comments":
            kinds = ("comments", "replies")
        elif phase == "incremental":
            # One catchup page, one old discussion page, alternately. No starvation.
            kinds = (
                ("catchup",)
                if self.get("incremental_turn", 0) % 2 == 0
                else ("comments", "replies")
            )
        else:
            return None
        marks = ",".join("?" for _ in kinds)
        row = self.db.execute(
            f"SELECT * FROM jobs WHERE done=0 AND kind IN ({marks}) "
            "ORDER BY checked,kind,key LIMIT 1",
            kinds,
        ).fetchone()
        return dict(row) if row else None

    def post(self, post_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if row is None:
            raise WeiboCollectError("invalid_checkpoint")
        return dict(row)

    def stage(
        self,
        job: dict[str, Any],
        *,
        post_rows: list[dict[str, Any]] | None = None,
        nodes: list[dict[str, Any]] | None = None,
        roots: list[dict[str, Any]] | None = None,
        cursor: str = "",
        terminal: bool = False,
        expected: int | None = None,
        root_seen: bool = False,
        include_comments: bool = True,
        include_replies: bool = True,
        advance_page: bool = True,
        processed_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Packet assignments and candidate rows survive failed import and process restart."""
        with self.transaction():
            if self.pending() is not None:
                raise WeiboCollectError("invalid_checkpoint")
            if (
                advance_page
                and not terminal
                and cursor
                and self.db.execute(
                    "SELECT 1 FROM cursors WHERE kind=? AND key=? AND cursor=?",
                    (job["kind"], job["key"], cursor),
                ).fetchone()
            ):
                raise WeiboCollectError("pagination_stalled")
            native_ids = processed_ids or [node["id"] for node in nodes or []]
            page_digest = (
                hashlib.sha256(_dump(sorted(set(native_ids))).encode()).hexdigest()
                if advance_page and not terminal and native_ids
                else None
            )
            if (
                page_digest
                and self.db.execute(
                    "SELECT 1 FROM pages WHERE kind=? AND key=? AND digest=?",
                    (job["kind"], job["key"], page_digest),
                ).fetchone()
            ):
                raise WeiboCollectError("pagination_stalled")
            changed: set[tuple[str, str]] = set()
            for node in nodes or []:
                post_id = job["post_id"]
                encoded_node = _dump(node)
                found = self.db.execute(
                    "SELECT packet,root_id,post_id,length(node_json) FROM nodes WHERE id=?",
                    (node["id"],),
                ).fetchone()
                if found:
                    if found[1] != node["root_id"] or found[2] != post_id:
                        raise WeiboCollectError("author_mismatch")
                    packet = found[0]
                    self.db.execute(
                        "UPDATE packets SET chars=chars+? WHERE post_id=? AND seed=?",
                        (len(encoded_node) - found[3], post_id, packet),
                    )
                else:
                    # Tail summaries make assignment O(log packets), not a full thread
                    # scan for every new node. Assignment never changes after first save.
                    candidate = self.db.execute(
                        "SELECT seed,members,chars FROM packets WHERE post_id=? "
                        "ORDER BY sequence DESC LIMIT 1",
                        (post_id,),
                    ).fetchone()
                    packet = node["id"]
                    if (
                        candidate
                        and candidate[1] < PACKET_NODES
                        and candidate[2] + len(encoded_node) < 80_000
                    ):
                        packet = candidate[0]
                    self.db.execute(
                        "INSERT INTO packets(post_id,seed,members,chars) VALUES(?,?,1,?) "
                        "ON CONFLICT(post_id,seed) DO UPDATE SET "
                        "members=members+1,chars=chars+excluded.chars",
                        (post_id, packet, len(encoded_node)),
                    )
                self.db.execute(
                    "INSERT INTO nodes(post_id,id,root_id,packet,node_json) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(post_id,id) DO UPDATE SET node_json=excluded.node_json",
                    (post_id, node["id"], node["root_id"], packet, encoded_node),
                )
                changed.add((post_id, packet))
            value = {
                "job": job,
                "post_rows": post_rows or [],
                "logical_packets": sorted(changed),
                "roots": roots or [],
                "cursor": cursor,
                "terminal": terminal,
                "expected": expected,
                "root_seen": root_seen,
                "include_comments": include_comments,
                "include_replies": include_replies,
                "advance_page": advance_page,
                "processed_ids": processed_ids or [],
                "page_digest": page_digest,
                "publication_index": 0,
            }
            # Keep the full UTF-8 candidate bounded, even if the upstream page was legal.
            encoded = _dump(value)
            if len(encoded.encode("utf-8")) > 9 * 1024 * 1024:
                raise WeiboCollectError("row_too_large")
            self.db.execute("INSERT INTO pending VALUES(1,?)", (encoded,))
        return value

    def packet_rows(self, post_id: str, seed: str) -> list[dict[str, Any]]:
        """Physical fragments of one stable logical group; never load a whole thread."""
        members = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT node_json FROM nodes WHERE post_id=? AND packet=? ORDER BY rowid",
                (post_id, seed),
            )
        ]
        if not members or len(members) > PACKET_NODES:
            raise WeiboCollectError("invalid_checkpoint")
        post = self.post(post_id)
        groups: list[list[dict[str, Any]]] = []
        chars = 0
        for node in members:
            length = len(node["text"])
            if length > PACKET_CHARS:
                raise WeiboCollectError("row_too_large")
            if not groups or chars + length > PACKET_CHARS:
                groups.append([])
                chars = 0
            groups[-1].append(node)
            chars += length
        rows = [
            {
                "version": 2,
                "id": f"{post_id}:{group[0]['id']}",
                "packet_id": f"{post_id}:{seed}",
                "post_id": post_id,
                "post_author_id": self.get("uid"),
                "post_url": post["url"],
                "nodes": group,
            }
            for group in groups
        ]
        if len(_dump(rows).encode("utf-8")) > 9 * 1024 * 1024:
            raise WeiboCollectError("row_too_large")
        return rows

    def publications(self) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        """Complete logical groups per publication, bounded even after many edits."""
        value = self.pending()
        if value is None:
            return
        offset = value.get("publication_index", 0)
        index = 0
        if value["post_rows"]:
            if index >= offset:
                yield "posts", value["post_rows"]
            index += 1
        for post_id, seed in value["logical_packets"]:
            if index >= offset:
                yield "discussions", self.packet_rows(post_id, seed)
            index += 1

    def publication_succeeded(self) -> None:
        """Persist publication progress without advancing the upstream page cursor."""
        with self.transaction():
            value = self.pending()
            if value is None:
                raise WeiboCollectError("invalid_checkpoint")
            value["publication_index"] = value.get("publication_index", 0) + 1
            self.db.execute("UPDATE pending SET value=? WHERE singleton=1", (_dump(value),))

    def acknowledge(self, *, now: float | None = None) -> None:
        """Called only after all candidate lanes have returned completed publication."""
        stamp = time.time() if now is None else now
        value = self.pending()
        if value is None:
            return
        job = value["job"]
        with self.transaction():
            for row in value["post_rows"]:
                self.db.execute(
                    "INSERT OR REPLACE INTO posts VALUES(?,?,?,?)",
                    (row["id"], row["url"], row["published_at"], _dump(row)),
                )
                if value["include_comments"]:
                    self.db.execute(
                        "INSERT OR IGNORE INTO jobs(kind,key,post_id) VALUES('comments',?,?)",
                        (row["id"], row["id"]),
                    )
            for post_id, seed in value["logical_packets"]:
                unpublished = self.db.execute(
                    "SELECT id,root_id FROM nodes WHERE post_id=? AND packet=? AND published=0",
                    (post_id, seed),
                ).fetchall()  # At most one bounded packet, never a full discussion tree.
                comments = sum(1 for node in unpublished if node[0] == node[1])
                replies = len(unpublished) - comments
                self.set("comments_total", self.get("comments_total", 0) + comments)
                self.set("replies_total", self.get("replies_total", 0) + replies)
                self.db.execute(
                    "UPDATE nodes SET published=1 WHERE post_id=? AND packet=?", (post_id, seed)
                )
            if value["include_replies"]:
                for root in value["roots"]:
                    self.db.execute(
                        "INSERT INTO jobs(kind,key,post_id,author_id,expected) "
                        "VALUES('replies',?,?,?,?) "
                        "ON CONFLICT(kind,key) DO UPDATE SET expected=excluded.expected",
                        (root["id"], job["post_id"], root["author_id"], root.get("expected")),
                    )
            if value["advance_page"] and not value["terminal"] and value["cursor"]:
                try:
                    self.db.execute(
                        "INSERT INTO cursors VALUES(?,?,?)",
                        (job["kind"], job["key"], value["cursor"]),
                    )
                except sqlite3.IntegrityError:
                    raise WeiboCollectError("pagination_stalled") from None
            if value.get("page_digest"):
                self.db.execute(
                    "INSERT INTO pages VALUES(?,?,?)",
                    (job["kind"], job["key"], value["page_digest"]),
                )
            self.db.execute(
                "UPDATE jobs SET cursor=?,page=?,done=?,expected=COALESCE(?,expected),"
                "root_seen=MAX(root_seen,?),checked=?,page_done=? WHERE kind=? AND key=?",
                (
                    value["cursor"],
                    job["page"] + int(value["advance_page"]),
                    int(value["terminal"]),
                    value["expected"],
                    int(value["root_seen"]),
                    stamp,
                    "[]" if value["advance_page"] else _dump(value["processed_ids"]),
                    job["kind"],
                    job["key"],
                ),
            )
            if value["terminal"] and job["kind"] in {"comments", "replies"}:
                # Only a successfully published true terminal page can clear a prior
                # inaccessible-page issue; coincidental old counts are never proof.
                self.db.execute(
                    "DELETE FROM issues WHERE kind=? AND key=? AND code='inaccessible'",
                    (job["kind"], job["key"]),
                )
                self._verify_job(job["kind"], job["key"])
            self.db.execute("DELETE FROM pending")
            if self.get("history_phase") == "incremental":
                if job["kind"] == "catchup" and value["terminal"]:
                    self.set("incremental_since", self.get("catchup_until"))
                self.set("incremental_turn", self.get("incremental_turn", 0) + 1)
            self._advance()

    def _verify_job(self, kind: str, key: str) -> None:
        job = self.db.execute("SELECT * FROM jobs WHERE kind=? AND key=?", (kind, key)).fetchone()
        if job is None or job["expected"] is None:
            return
        if kind == "replies":
            observed = self.db.execute(
                "SELECT COUNT(*) FROM nodes WHERE post_id=? AND root_id=? AND id!=?",
                (job["post_id"], key, key),
            ).fetchone()[0]
            expected = job["expected"]
            # rootComment is context, not evidence that total_number includes the root.
            valid = observed == expected
        else:
            # Timeline totals commonly count both top-level comments and replies. Only
            # Verify after replies close; top-level-only totals cannot hide omissions.
            if self.db.execute(
                "SELECT 1 FROM jobs WHERE post_id=? AND kind='replies' AND done=0 LIMIT 1",
                (job["post_id"],),
            ).fetchone():
                return
            observed = self.db.execute(
                "SELECT COUNT(*) FROM nodes WHERE post_id=?", (job["post_id"],)
            ).fetchone()[0]
            expected = job["expected"]
            valid = observed == expected
        if valid:
            self.db.execute(
                "DELETE FROM issues WHERE kind=? AND key=? AND code='count_mismatch'", (kind, key)
            )
        else:
            self.db.execute(
                "INSERT INTO issues VALUES(?,?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET "
                "expected=excluded.expected,observed=excluded.observed "
                "WHERE issues.code='count_mismatch'",
                (kind, key, "count_mismatch", expected, observed),
            )

    def _advance(self) -> None:
        phase = self.get("history_phase")
        if (
            phase == "posts"
            and not self.db.execute("SELECT 1 FROM jobs WHERE kind='posts' AND done=0").fetchone()
        ):
            self.set("history_phase", "comments")
            phase = "comments"
        if (
            phase == "comments"
            and not self.db.execute(
                "SELECT 1 FROM jobs WHERE kind IN ('comments','replies') AND done=0 LIMIT 1"
            ).fetchone()
        ):
            self.set("history_phase", "verifying")

    def verify(self) -> bool:
        with self.transaction():
            for row in self.db.execute(
                "SELECT kind,key FROM jobs WHERE done=1 AND kind IN ('comments','replies')"
            ):
                self._verify_job(row[0], row[1])
            if self.pending() or self.db.execute("SELECT 1 FROM issues LIMIT 1").fetchone():
                return False
            if self.db.execute("SELECT 1 FROM jobs WHERE done=0 LIMIT 1").fetchone():
                return False
            self.set("history_phase", "incremental")
            self.set("history_complete", True)
            self.set("incremental_turn", 0)
            self.set("incremental_since", self.get("snapshot_at"))
            self._reset_catchup()
        return True

    def _reset_catchup(self) -> None:
        lower = datetime.fromisoformat(self.get("incremental_since", self.get("snapshot_at")))
        self.set("catchup_until", max(datetime.now(UTC), lower).isoformat())
        self.set("sweep_seeded", False)
        self.db.execute("DELETE FROM cursors WHERE kind='catchup'")
        self.db.execute("DELETE FROM pages WHERE kind='catchup'")
        self.db.execute("INSERT OR REPLACE INTO jobs(kind,key) VALUES('catchup','timeline')")

    def prepare_incremental(self) -> None:
        """Restart finished sweeps fairly; the initial catchup lower bound is the fixed T0."""
        if self.get("history_phase") != "incremental":
            return
        with self.transaction():
            catchup = self.db.execute("SELECT done FROM jobs WHERE kind='catchup'").fetchone()
            discussions_pending = self.db.execute(
                "SELECT 1 FROM jobs WHERE kind IN ('comments','replies') AND done=0 LIMIT 1"
            ).fetchone()
            if catchup is None or (catchup[0] and not discussions_pending):
                self._reset_catchup()
            # One oldest completed post/root per incremental cycle, not an endless
            # fast restart of completed lanes while another lane is still active.
            if not self.get("sweep_seeded"):
                row = self.db.execute(
                    "SELECT kind,key FROM jobs WHERE kind IN ('comments','replies') AND done=1 "
                    "ORDER BY checked,kind,key LIMIT 1"
                ).fetchone()
                if row:
                    self.db.execute(
                        "UPDATE jobs SET done=0,cursor='',page=1,page_done='[]' "
                        "WHERE kind=? AND key=?",
                        tuple(row),
                    )
                    self.db.execute("DELETE FROM cursors WHERE kind=? AND key=?", tuple(row))
                    self.db.execute("DELETE FROM pages WHERE kind=? AND key=?", tuple(row))
                self.set("sweep_seeded", True)
            # If a lane has no jobs, fall back rather than idling forever.
            if self.job() is None:
                self.set("incremental_turn", self.get("incremental_turn", 0) + 1)

    def inaccessible(self, job: dict[str, Any]) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO issues VALUES(?,?,?,NULL,NULL)",
                (job["kind"], job["key"], "inaccessible"),
            )
            self.db.execute(
                "UPDATE jobs SET done=1,checked=? WHERE kind=? AND key=?",
                (time.time(), job["kind"], job["key"]),
            )
            self._advance()

    def status(self) -> dict[str, Any]:
        def count(sql: str) -> int:
            return int(self.db.execute(sql).fetchone()[0])

        phase = self.get("history_phase")
        paused = self.get("paused", False)
        blocked = self.get("block_reason")
        oldest = self.db.execute("SELECT MIN(published) FROM posts").fetchone()[0]
        return {
            "history_phase": "blocked" if blocked else "paused" if paused else phase,
            "history_complete": self.get("history_complete", False),
            "history_started_at": self.get("history_started_at"),
            "snapshot_at": self.get("snapshot_at"),
            "posts_total": count("SELECT COUNT(*) FROM posts"),
            "comments_total": self.get("comments_total", 0),
            "replies_total": self.get("replies_total", 0),
            "pending_posts": count(
                "SELECT COUNT(*) FROM jobs WHERE kind IN ('posts','catchup') AND done=0"
            ),
            "pending_comments": count("SELECT COUNT(*) FROM jobs WHERE kind='comments' AND done=0"),
            "pending_replies": count("SELECT COUNT(*) FROM jobs WHERE kind='replies' AND done=0"),
            "index_pending": int(self.pending() is not None),
            "inaccessible": count("SELECT COUNT(*) FROM issues WHERE code='inaccessible'"),
            "count_mismatches": count("SELECT COUNT(*) FROM issues WHERE code='count_mismatch'"),
            "oldest_date": oldest,
            "paused": paused,
            "coverage_message": "历史队列已核验，正在增量复查。"
            if self.get("history_complete")
            else "历史尚未完整核验；批次完成不代表全历史完成。",
            "measurement": self.get("measurement", {}),
        }
