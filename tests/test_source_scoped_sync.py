"""文档作用域发布：内存实现及真实隔离 pgvector 的同一套契约回归。"""

from __future__ import annotations

import asyncio
import json
import os
import weakref
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import asyncpg
import pytest

from jianshanagent.core.db import MIGRATIONS_DIR, Database
from jianshanagent.plugins.knowledge.store import (
    _TERMS_WRITE_LOCK,
    _UPSERT_BATCH,
    KbChunk,
    KnowledgeStore,
    MemoryKnowledgeStore,
    PostgresKnowledgeStore,
    Record,
)
from jianshanagent.plugins.knowledge.terms import extract_terms

FP = "scoped:4096"
UNIT = [1.0] + [0.0] * 4095


@pytest.fixture
async def scoped_pg() -> AsyncIterator[PostgresKnowledgeStore]:
    dsn = os.environ.get("TEST_DSN")
    if not dsn or urlsplit(dsn).hostname not in ("localhost", "127.0.0.1", "::1"):
        pytest.skip("需要显式设置隔离本地 TEST_DSN，不能用 mock 代替真实 PG")
    schema = "test_source_scoped_" + uuid4().hex
    base, _, query = dsn.partition("?")
    settings = dict(parse_qsl(query, keep_blank_values=True))
    settings["search_path"] = schema + ",public"
    db = Database(base + "?" + urlencode(settings))
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await db.connect()
        await db.pool.execute((MIGRATIONS_DIR / "0003_knowledge.sql").read_text())
        await db.pool.execute("ALTER TABLE kb_chunks ADD COLUMN wiki TEXT NOT NULL DEFAULT ''")
        yield PostgresKnowledgeStore(db)
    finally:
        await db.close()
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> KnowledgeStore:
    if request.param == "memory":
        return MemoryKnowledgeStore()
    return request.getfixturevalue("scoped_pg")


def chunk(
    doc: str, text: str = "alpha shared", *, wiki: str = "source", part: str = "0"
) -> KbChunk:
    return KbChunk(
        wiki=wiki,
        chunk_id=f"{wiki}:{doc}:{part}",
        doc_key=f"{wiki}:{doc}",
        kind="post",
        title=doc,
        text=text,
    )


async def term_counts(store: KnowledgeStore) -> dict[str, int]:
    if isinstance(store, MemoryKnowledgeStore):
        return dict(store._df)
    assert isinstance(store, PostgresKnowledgeStore)
    rows = await store._db.pool.fetch("SELECT term, df FROM kb_terms ORDER BY term")
    return {row["term"]: row["df"] for row in rows}


async def assert_terms_match_chunks(store: KnowledgeStore) -> None:
    if isinstance(store, MemoryKnowledgeStore):
        expected: dict[str, int] = {}
        for item in store._chunks.values():
            for term in extract_terms(item.text):
                expected[term] = expected.get(term, 0) + 1
    else:
        assert isinstance(store, PostgresKnowledgeStore)
        rows = await store._db.pool.fetch(
            "SELECT term, count(*) AS df FROM kb_chunks, unnest(terms) AS term GROUP BY term"
        )
        expected = {row["term"]: row["df"] for row in rows}
    actual = await term_counts(store)
    assert actual == expected
    assert all(df > 0 for df in actual.values())


async def test_scope_preserves_other_documents_sources_and_vectors(store: KnowledgeStore) -> None:
    a, b, wiki = chunk("a"), chunk("b", "bravo shared"), chunk("a", "wiki", wiki="huiji")
    await store.sync([(a, UNIT), (b, UNIT)], fingerprint=FP, wiki="source")
    await store.sync([(wiki, UNIT)], fingerprint=FP, wiki="huiji")
    before_other = (await store.info()).wikis["huiji"]
    edited = replace(a, text="edited shared")
    report = await store.sync_documents(
        iter([(edited, UNIT)]), fingerprint=FP, wiki="source", doc_keys=[a.doc_key]
    )
    assert (report.total, report.upserted, report.unchanged, report.deleted) == (1, 1, 0, 0)
    stored = await store.get([a.chunk_id, b.chunk_id, wiki.chunk_id])
    assert {c.chunk_id: c for c in stored} == {c.chunk_id: c for c in (edited, b, wiki)}
    info = await store.info()
    assert info.chunks == 3 and info.with_vectors == 3
    assert info.wikis["huiji"] == before_other
    assert info.wikis["source"].chunks == 2
    assert info.wikis["source"].fingerprint == FP
    assert info.wikis["source"].average_chars == (len(edited.text) + len(b.text)) / 2
    await assert_terms_match_chunks(store)


async def test_noop_preserves_row_and_global_term_counts(store: KnowledgeStore) -> None:
    a = chunk("a", "alpha alpha shared")
    await store.sync([(a, UNIT)], fingerprint=FP, wiki="source")
    before_terms = await term_counts(store)
    updated_at = None
    if isinstance(store, PostgresKnowledgeStore):
        updated_at = await store._db.pool.fetchval(
            "SELECT updated_at FROM kb_chunks WHERE chunk_id = $1", a.chunk_id
        )
        # scoped 路径不能调用旧的 TRUNCATE 重算；真实 trigger 验证而非 mock。
        await store._db.pool.execute(
            "CREATE FUNCTION forbid_term_truncate() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'scoped sync must not truncate terms'; END $$; "
            "CREATE TRIGGER no_truncate BEFORE TRUNCATE ON kb_terms "
            "EXECUTE FUNCTION forbid_term_truncate()"
        )
    report = await store.sync_documents(
        [(a, UNIT)], fingerprint=FP, wiki="source", doc_keys=[a.doc_key]
    )
    assert (report.total, report.upserted, report.unchanged, report.deleted) == (1, 0, 1, 0)
    assert await term_counts(store) == before_terms
    if isinstance(store, PostgresKnowledgeStore):
        assert (
            await store._db.pool.fetchval(
                "SELECT updated_at FROM kb_chunks WHERE chunk_id = $1", a.chunk_id
            )
            == updated_at
        )
    await assert_terms_match_chunks(store)


async def test_edit_delete_and_empty_restore_drop_old_terms(store: KnowledgeStore) -> None:
    a, a2, b = chunk("a", "obsolete shared"), chunk("a", "removed", part="1"), chunk("b")
    await store.sync([(a, UNIT), (a2, UNIT), (b, None)], fingerprint=FP, wiki="source")
    edited = replace(a, text="novel shared")
    report = await store.sync_documents(
        [(edited, None)], fingerprint=FP, wiki="source", doc_keys=[a.doc_key]
    )
    assert report.deleted == 1 and report.with_vectors == 0
    assert (await store.info()).wikis["source"].with_vectors == 0
    assert "obsolete" not in await term_counts(store)
    assert "removed" not in await term_counts(store)
    assert (await term_counts(store))["shared"] == 2
    report = await store.sync_documents(
        [], fingerprint=FP, wiki="source", doc_keys=[a.doc_key, "source:never-existed"]
    )
    assert report.total == 0 and report.deleted == 1
    assert await store.get([a.chunk_id, a2.chunk_id, b.chunk_id]) == [b]
    assert "novel" not in await term_counts(store)
    await assert_terms_match_chunks(store)
    # 重复恢复此前不存在的scope同样幂等，不误删 b。
    report = await store.sync_documents([], fingerprint=FP, wiki="source", doc_keys=[a.doc_key])
    assert report.deleted == 0 and (await store.info()).chunks == 1


async def test_empty_scope_is_noop_and_empty_namespace_stats(store: KnowledgeStore) -> None:
    before = await store.info()
    report = await store.sync_documents([], fingerprint=FP, wiki="source", doc_keys=[])
    assert report.total == report.deleted == 0 and await store.info() == before
    await store.sync_documents([], fingerprint=FP, wiki="source", doc_keys=["source:absent"])
    info = (await store.info()).wikis["source"]
    assert info.chunks == info.with_vectors == info.average_chars == 0
    assert info.fingerprint is None
    await assert_terms_match_chunks(store)


@pytest.mark.parametrize(
    ("wiki", "keys"),
    [
        ("", ["source:a"]),
        ("source:other", ["source:other:a"]),
        ("source", ["other:a"]),
        ("source", ["a"]),
        ("source", ["source:"]),
        ("source", "source:a"),
    ],
)
async def test_invalid_scope_rejected(store: KnowledgeStore, wiki: str, keys: list[str]) -> None:
    a = chunk("a")
    await store.sync([(a, UNIT)], fingerprint=FP, wiki="source")
    before = await store.info()
    with pytest.raises(ValueError):
        await store.sync_documents([], fingerprint=FP, wiki=wiki, doc_keys=keys)
    assert await store.info() == before
    assert await store.get([a.chunk_id]) == [a]
    await assert_terms_match_chunks(store)


@pytest.mark.parametrize("invalid", ["doc", "wiki", "prefix", "collision", "duplicate"])
async def test_record_scope_violations_rollback(store: KnowledgeStore, invalid: str) -> None:
    a, b = chunk("a"), chunk("b", "preserved")
    await store.sync([(a, UNIT), (b, UNIT)], fingerprint=FP, wiki="source")
    other = {
        "doc": chunk("outside"),
        "wiki": replace(a, wiki="other"),
        "prefix": replace(a, chunk_id="other:a:0"),
        "collision": replace(a, chunk_id=b.chunk_id),
        "duplicate": a,
    }[invalid]
    before = await store.info()
    with pytest.raises(ValueError):
        await store.sync_documents(
            [(a, None), (other, UNIT)], fingerprint=FP, wiki="source", doc_keys=[a.doc_key]
        )
    assert await store.info() == before
    assert await store.get([a.chunk_id, b.chunk_id]) == [a, b]
    await assert_terms_match_chunks(store)


async def test_stream_failure_after_batch_rolls_back(store: KnowledgeStore) -> None:
    a = chunk("a", "original")
    await store.sync([(a, UNIT)], fingerprint=FP, wiki="source")
    before = await store.info()

    def records() -> Iterator[Record]:
        for part in range(_UPSERT_BATCH + 1):
            yield chunk("a", "candidate", part=str(part)), UNIT
        raise RuntimeError("input failed after a full batch")

    with pytest.raises(RuntimeError, match="input failed"):
        await store.sync_documents(records(), fingerprint=FP, wiki="source", doc_keys=[a.doc_key])
    assert await store.info() == before
    assert await store.get([a.chunk_id, "source:a:100"]) == [a]
    await assert_terms_match_chunks(store)


async def test_mixed_fingerprint_metadata_is_honest(store: KnowledgeStore) -> None:
    a, b = chunk("a"), chunk("b")
    await store.sync([(a, UNIT), (b, UNIT)], fingerprint=FP, wiki="source")
    await store.sync_documents(
        [(a, UNIT)], fingerprint="different:4096", wiki="source", doc_keys=[a.doc_key]
    )
    info = (await store.info()).wikis["source"]
    assert info.with_vectors == 2 and info.fingerprint is None
    await store.sync_documents([], fingerprint=FP, wiki="source", doc_keys=[a.doc_key])
    assert (await store.info()).wikis["source"].fingerprint == FP


async def test_sql_metacharacters_are_data_not_query(store: KnowledgeStore) -> None:
    a = chunk("a'; DROP TABLE kb_chunks; --", "quoted payload")
    await store.sync_documents([(a, UNIT)], fingerprint=FP, wiki="source", doc_keys=[a.doc_key])
    assert await store.get([a.chunk_id]) == [a]
    await assert_terms_match_chunks(store)


async def test_memory_resource_usage_fails_explicitly() -> None:
    with pytest.raises(RuntimeError, match="无法测量"):
        await MemoryKnowledgeStore().resource_usage(["source"])


async def test_pg_resource_usage_includes_real_vectors_and_is_readonly(
    scoped_pg: PostgresKnowledgeStore,
) -> None:
    a, other = chunk("a"), chunk("other", "other", wiki="other")
    await scoped_pg.sync([(a, UNIT)], fingerprint=FP, wiki="source")
    await scoped_pg.sync([(other, UNIT)], fingerprint=FP, wiki="other")
    before = await scoped_pg.info()
    usage = await scoped_pg.resource_usage(["source"])
    assert set(usage) == {
        "source_rows_bytes",
        "database_bytes",
        "wal_position_bytes",
        "pg_started_at",
    }
    assert all(isinstance(value, int) for value in usage.values())
    # 4096维float32本体已16KiB；不能只算正文而漏掉PG向量副本。
    assert usage["source_rows_bytes"] >= 4096 * 4
    assert usage["database_bytes"] > usage["source_rows_bytes"]
    assert usage["wal_position_bytes"] > 0
    expected = await scoped_pg._db.pool.fetchval(
        "SELECT sum(pg_column_size(c)) FROM kb_chunks c WHERE wiki = $1", "source"
    )
    assert usage["source_rows_bytes"] == expected
    combined = await scoped_pg.resource_usage(["source", "other"])
    assert combined["source_rows_bytes"] > usage["source_rows_bytes"]
    assert combined["wal_position_bytes"] >= usage["wal_position_bytes"]
    exact_started = await scoped_pg._db.pool.fetchval(
        "SELECT (extract(epoch from pg_postmaster_start_time()) * 1000000)::bigint"
    )
    assert type(usage["pg_started_at"]) is int
    assert combined["pg_started_at"] == usage["pg_started_at"] == exact_started
    assert usage["pg_started_at"] > 1_000_000_000_000_000  # epoch微秒，不能误用epoch秒。
    assert (await scoped_pg.resource_usage([]))["source_rows_bytes"] == 0
    assert await scoped_pg.info() == before
    await assert_terms_match_chunks(scoped_pg)


async def test_pg_resource_permission_error_is_not_reported_as_zero(
    scoped_pg: PostgresKnowledgeStore,
) -> None:
    db = scoped_pg._db
    role = "test_scoped_reader_" + uuid4().hex
    schema = await db.pool.fetchval("SELECT current_schema()")
    assert schema.startswith("test_source_scoped_") and schema.replace("_", "").isalnum()
    limited_db = Database(db._dsn + "&role=" + role)
    await db.pool.execute(f"CREATE ROLE {role}")
    try:
        await db.pool.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        await limited_db.connect()
        # 真实只读角色没有SELECT本来源表权限；资源测量必须显式失败，不能返回全零。
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await PostgresKnowledgeStore(limited_db).resource_usage(["source"])
    finally:
        await limited_db.close()
        await db.pool.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {role}")
        await db.pool.execute(f"DROP ROLE {role}")


async def test_pg_stream_keeps_only_current_vector_in_python(
    scoped_pg: PostgresKnowledgeStore,
) -> None:
    class TrackedVector(list[float]):
        pass

    references: list[weakref.ReferenceType[TrackedVector]] = []

    def records() -> Iterator[Record]:
        for part in range(_UPSERT_BATCH * 2 + 1):
            vector = TrackedVector(UNIT)
            references.append(weakref.ref(vector))
            # list(records) 或缓存原始向量会在这里失败；只允许循环/生成器当前引用。
            assert sum(ref() is not None for ref in references) <= 2
            yield chunk("stream", part=str(part)), vector

    report = await scoped_pg.sync_documents(
        records(), fingerprint=FP, wiki="source", doc_keys=["source:stream"]
    )
    assert report.total == report.upserted == report.with_vectors == _UPSERT_BATCH * 2 + 1
    assert all(ref() is None for ref in references)
    await assert_terms_match_chunks(scoped_pg)


async def test_pg_bad_vector_stops_at_finite_batch(scoped_pg: PostgresKnowledgeStore) -> None:
    produced = 0

    def records() -> Iterator[Record]:
        nonlocal produced
        for part in range(_UPSERT_BATCH * 10):
            produced += 1
            yield chunk("stream", part=str(part)), ([0.0] if part == 1 else UNIT)

    before = await scoped_pg.info()
    with pytest.raises(asyncpg.DataError):
        await scoped_pg.sync_documents(
            records(), fingerprint=FP, wiki="source", doc_keys=["source:stream"]
        )
    assert produced == _UPSERT_BATCH
    assert await scoped_pg.info() == before
    assert await scoped_pg.get(["source:stream:0"]) == []
    await assert_terms_match_chunks(scoped_pg)


async def test_pg_duplicate_across_batches_is_atomic(scoped_pg: PostgresKnowledgeStore) -> None:
    a = chunk("stream", "original")
    await scoped_pg.sync([(a, UNIT)], fingerprint=FP, wiki="source")

    def records() -> Iterator[Record]:
        for part in range(_UPSERT_BATCH):
            yield chunk("stream", "candidate", part=str(part)), UNIT
        yield a, UNIT

    before = await scoped_pg.info()
    with pytest.raises(ValueError, match="重复"):
        await scoped_pg.sync_documents(
            records(), fingerprint=FP, wiki="source", doc_keys=[a.doc_key]
        )
    assert await scoped_pg.info() == before
    assert await scoped_pg.get([a.chunk_id]) == [a]
    await assert_terms_match_chunks(scoped_pg)


async def test_pg_corrupt_df_fails_without_committing_negative_counts(
    scoped_pg: PostgresKnowledgeStore,
) -> None:
    a = chunk("a", "original")
    await scoped_pg.sync([(a, UNIT)], fingerprint=FP, wiki="source")
    await scoped_pg._db.pool.execute("DELETE FROM kb_terms WHERE term = $1", "original")
    before = await scoped_pg.info()
    with pytest.raises(ValueError, match="统计不一致"):
        await scoped_pg.sync_documents([], fingerprint=FP, wiki="source", doc_keys=[a.doc_key])
    assert await scoped_pg.info() == before
    assert await scoped_pg.get([a.chunk_id]) == [a]
    assert not await scoped_pg._db.pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM kb_terms WHERE df < 0)"
    )


async def test_pg_constraint_failure_rolls_back_every_table(
    scoped_pg: PostgresKnowledgeStore,
) -> None:
    a = chunk("a", "original")
    await scoped_pg.sync([(a, UNIT)], fingerprint=FP, wiki="source")
    before_info = await scoped_pg.info()
    before_terms = await term_counts(scoped_pg)
    # 故意让发布至主表后失败，临时表不携带这条新增主表约束。
    await scoped_pg._db.pool.execute(
        "ALTER TABLE kb_chunks ADD CONSTRAINT reject_candidate CHECK (text <> 'candidate')"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await scoped_pg.sync_documents(
            [(replace(a, text="candidate"), UNIT)],
            fingerprint=FP,
            wiki="source",
            doc_keys=[a.doc_key],
        )
    assert await scoped_pg.info() == before_info
    assert await term_counts(scoped_pg) == before_terms
    assert await scoped_pg.get([a.chunk_id]) == [a]
    # 失败后连接池仍可继续发布，临时表随事务回滚清理。
    await scoped_pg.sync_documents(
        [(replace(a, text="recovered"), UNIT)],
        fingerprint=FP,
        wiki="source",
        doc_keys=[a.doc_key],
    )
    await assert_terms_match_chunks(scoped_pg)


async def test_pg_full_and_scoped_writes_share_transaction_lock(
    scoped_pg: PostgresKnowledgeStore,
) -> None:
    a, b, wiki = chunk("a", "old shared"), chunk("b"), chunk("w", "wiki shared", wiki="huiji")
    await scoped_pg.sync([(a, UNIT), (b, UNIT)], fingerprint=FP, wiki="source")
    await scoped_pg.sync([(wiki, UNIT)], fingerprint=FP, wiki="huiji")
    pool = scoped_pg._db.pool
    tasks: list[asyncio.Task] = []
    try:
        async with pool.acquire() as blocker, blocker.transaction():
            await blocker.execute("SELECT pg_advisory_xact_lock($1)", _TERMS_WRITE_LOCK)
            tasks = [
                asyncio.create_task(
                    scoped_pg.sync_documents(
                        [(replace(a, text="new shared"), UNIT)],
                        fingerprint=FP,
                        wiki="source",
                        doc_keys=[a.doc_key],
                    )
                ),
                asyncio.create_task(
                    scoped_pg.sync(
                        [(replace(wiki, text="fresh shared"), UNIT)],
                        fingerprint=FP,
                        wiki="huiji",
                    )
                ),
            ]
            async with asyncio.timeout(10):
                while True:
                    waiting = await pool.fetchval(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND classid = $1 AND objid = $2 AND NOT granted "
                        "AND database = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database())",
                        _TERMS_WRITE_LOCK >> 32,
                        _TERMS_WRITE_LOCK & 0xFFFFFFFF,
                    )
                    if waiting >= 2:
                        break
                    await asyncio.sleep(0.01)
            assert all(not task.done() for task in tasks)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert (await scoped_pg.info()).chunks == 3
    assert (await term_counts(scoped_pg))["shared"] == 3
    assert "old" not in await term_counts(scoped_pg)
    assert "wiki" not in await term_counts(scoped_pg)
    await assert_terms_match_chunks(scoped_pg)


async def test_pg_keep_only_then_scoped_sync_retains_consistent_terms(
    scoped_pg: PostgresKnowledgeStore,
) -> None:
    a, other = chunk("a"), chunk("b", "removed shared", wiki="other")
    await scoped_pg.sync([(a, UNIT)], fingerprint=FP, wiki="source")
    await scoped_pg.sync([(other, UNIT)], fingerprint=FP, wiki="other")
    assert await scoped_pg.keep_only(["source"]) == 1
    await scoped_pg.sync_documents(
        [(replace(a, text="changed shared"), UNIT)],
        fingerprint=FP,
        wiki="source",
        doc_keys=[a.doc_key],
    )
    await assert_terms_match_chunks(scoped_pg)
    assert set((await scoped_pg.info()).wikis) == {"source"}
    value = await scoped_pg._db.pool.fetchval(
        "SELECT value FROM kb_meta WHERE key = $1", "stats:source"
    )
    assert json.loads(value)["chunks"] == 1
