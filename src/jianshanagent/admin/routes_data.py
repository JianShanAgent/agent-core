"""数据：知识库和图库（浏览、试检索、以图搜图），会话与记忆，群聊记录，备注名。"""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any

from aiohttp import web

from jianshanagent.admin.core import (
    Admin,
    fail,
    flag_param,
    int_param,
    read_body,
    reply,
    text_param,
)
from jianshanagent.admin.queries import CHAT_DAYS, SESSION_DAYS
from jianshanagent.core.embedding import EmbeddingError
from jianshanagent.core.images import ImageError
from jianshanagent.core.trace import brief
from jianshanagent.plugins.knowledge.plugin import TYPE_NAMES

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: 会话清单、群聊清单的「最近多少天」最多能填多少（十年；0 = 不限时间）
_MAX_DAYS = 3650


def _message_view(row: dict[str, Any]) -> dict[str, Any]:
    """对话历史里的一条（按模型接口的原始格式存的）→ 页面上好显示的样子。"""
    message = row["message"] if isinstance(row["message"], dict) else {}
    content = message.get("content")
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        calls.append({"name": function.get("name"), "arguments": function.get("arguments")})
    return {
        "id": row["id"],
        "role": row["role"],
        "created_at": row["created_at"],
        "content": content if isinstance(content, str) else str(content or ""),
        "tool_calls": calls,
        "reasoning": bool(message.get("reasoning_content")),
    }


def routes(admin: Admin) -> list[web.RouteDef]:
    labelled = admin.labelled

    # ---- 知识库 ----

    async def kb_summary(_request: web.Request) -> web.Response:
        retriever = admin.service("knowledge")
        image_search = admin.service("image_search")
        result: dict[str, Any] = {
            "available": retriever is not None,
            "image_search": image_search is not None,
            "type_names": TYPE_NAMES,
            "store": None,
            "images": None,
            "types": [],
            "image_kinds": [],
            "settings": None,
            # 挂着的各个 wiki：库里各有多少、自动更新的状态
            "wikis": [],
        }
        if not admin.has_database:
            return reply(result)
        summary = await admin.queries.kb_summary()
        result["types"], result["image_kinds"] = summary["types"], summary["image_kinds"]
        #: 各个 wiki 在库里的概况：wiki 的 id → 切片的、图片的
        chunk_info: dict[str, Any] = {}
        image_info: dict[str, Any] = {}
        if retriever is not None:
            info = await retriever.store.info()
            chunk_info = info.wikis
            embedder = retriever.embedder
            fingerprint = embedder.provider.fingerprint if embedder is not None else None
            result["store"] = {
                "chunks": info.chunks,
                "with_vectors": info.with_vectors,
                "fingerprint": info.fingerprint,
                "loaded_at": info.loaded_at,
                "average_chars": info.average_chars,
                "query_fingerprint": fingerprint,
                "vectors_usable": fingerprint is not None and info.has_vectors_of(fingerprint),
            }
            settings = retriever.settings
            result["settings"] = {"top_k": settings.top_k, "keyword_slots": settings.keyword_slots}
        if image_search is not None:
            info = await image_search.store.info()
            image_info = info.wikis
            result["images"] = {
                "images": info.images,
                "with_vectors": info.with_vectors,
                "fingerprint": info.fingerprint,
                "preprocess": info.preprocess,
                "loaded_at": info.loaded_at,
                "min_score": image_search.settings.min_score,
                "sure_score": image_search.settings.sure_score,
                "sure_margin": image_search.settings.sure_margin,
            }
        for wiki in admin.config.wikis:
            chunks, images = chunk_info.get(wiki.id), image_info.get(wiki.id)
            result["wikis"].append(
                {
                    "id": wiki.id,
                    "label": str(wiki.raw.get("label") or wiki.id),
                    "chunks": chunks.chunks if chunks else 0,
                    "with_vectors": chunks.with_vectors if chunks else 0,
                    "fingerprint": chunks.fingerprint if chunks else None,
                    "loaded_at": chunks.loaded_at if chunks else None,
                    "images": images.images if images else 0,
                    # 自动更新的状态（服务器上的 kbsync 服务写的）；它没跑过就是 None
                    "sync": await admin.queries.kb_sync(wiki.id),
                }
            )
        return reply(result)

    async def kb_chunks(request: web.Request) -> web.Response:
        return reply(
            await admin.queries.kb_chunks(
                limit=int_param(request, "limit", 30, 1, 200),
                offset=int_param(request, "offset", 0, 0, 10**7),
                kind=text_param(request, "type"),
                search=text_param(request, "q"),
                wiki=text_param(request, "wiki"),
            )
        )

    async def kb_chunk(request: web.Request) -> web.Response:
        found = await admin.queries.kb_chunk(request.match_info["id"])
        if found is None:
            fail(404, "没有这个切片（知识库可能重新导入过）")
        return reply(found)

    async def kb_search(request: web.Request) -> web.Response:
        """用和机器人一样的检索查一次：看一个问题会捞到哪几片、各是哪一路找到的。"""
        retriever = admin.service("knowledge")
        if retriever is None:
            fail(503, "知识库检索没有启用（要有数据库，plugin.knowledge 的 retrieval.enabled）")
        body = await read_body(request)
        query = str(body.get("query") or "").strip()
        if not query:
            fail(400, "要查的话是空的")
        top_k = body.get("top_k")
        result = await retriever.search(query, top_k=int(top_k) if top_k else None)
        return reply(
            {
                "query": query,
                "mode": result.mode,
                "vector_skipped": result.vector_skipped,
                "seconds": result.seconds,
                "hits": [
                    {
                        "chunk_id": hit.chunk.chunk_id,
                        "wiki": hit.chunk.wiki,
                        "type": hit.chunk.kind,
                        "title": hit.chunk.title,
                        "section_path": list(hit.chunk.section_path),
                        "date": hit.chunk.date,
                        "date_inferred": hit.chunk.date_inferred,
                        "url": hit.chunk.url,
                        "chars": len(hit.chunk.text),
                        "text": hit.chunk.text,
                        "vector_rank": hit.vector_rank,
                        "keyword_rank": hit.keyword_rank,
                    }
                    for hit in result.hits
                ],
            }
        )

    async def kb_images(request: web.Request) -> web.Response:
        return reply(
            await admin.queries.kb_images(
                limit=int_param(request, "limit", 48, 1, 200),
                offset=int_param(request, "offset", 0, 0, 10**7),
                kind=text_param(request, "kind"),
                search=text_param(request, "q"),
                wiki=text_param(request, "wiki"),
            )
        )

    async def kb_image_search(request: web.Request) -> web.Response:
        """传一张图，用和机器人一样的以图搜图查一次。"""
        image_search = admin.service("image_search")
        if image_search is None:
            fail(503, "以图搜图没有启用（要有数据库和嵌入接口）")
        body = await read_body(request)
        raw = str(body.get("image") or "")
        encoded = raw.split(",", 1)[1] if raw.startswith("data:") and "," in raw else raw
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            fail(400, "图片的内容不完整")
        if not data or len(data) > _MAX_IMAGE_BYTES:
            fail(400, f"图片是空的，或者超过 {_MAX_IMAGE_BYTES // 1024 // 1024} MB")
        started = time.perf_counter()
        try:
            hits = await image_search.search(data, top_k=int(body.get("top_k") or 0) or None)
        except ImageError as exc:
            fail(400, f"这不是能识别的图片：{exc}")
        except EmbeddingError as exc:
            fail(502, f"嵌入接口出错：{brief(exc)}")
        return reply(
            {
                "seconds": time.perf_counter() - started,
                "hits": [
                    {
                        "image_id": hit.image_id,
                        "wiki": hit.wiki,
                        "label": hit.label,
                        "kind": hit.kind,
                        "page_title": hit.page_title,
                        "url": hit.url,
                        "image_url": hit.image_url,
                        "score": hit.score,
                        "sure": hit.sure,
                        "text": hit.text,
                    }
                    for hit in hits
                ],
            }
        )

    # ---- 会话、记忆 ----

    async def sessions(request: web.Request) -> web.Response:
        # days：只列最近这么多天有动静的会话，0 = 不限时间（要把几张表整个过一遍，页面上不自动刷新）
        days = int_param(request, "days", SESSION_DAYS, 0, _MAX_DAYS)
        found = await admin.queries.sessions(
            limit=int_param(request, "limit", 50, 1, 200),
            offset=int_param(request, "offset", 0, 0, 10**7),
            search=text_param(request, "q"),
            days=days,
        )
        found["items"] = await labelled(found["items"])
        return reply({**found, "days": days})

    async def session(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        queries = admin.queries
        found = await queries.session(key)
        found["messages"] = [_message_view(row) for row in found["messages"]]
        found["traces"] = await labelled(await queries.traces(limit=20, session_key=key))
        # 昵称不跟着会话存：从这个会话最近的处理记录里取（平台给了才有）
        found["user_name"] = next(
            (row["user_name"] for row in found["traces"] if row.get("user_name")), None
        )
        (labelled_row,) = await labelled([found])
        return reply(labelled_row)

    async def clear_history(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        return reply({"deleted": await admin.queries.clear_history(key)})

    async def clear_memories(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        return reply({"deleted": await admin.queries.clear_memories(key)})

    async def delete_memory(request: web.Request) -> web.Response:
        raw = request.match_info["id"]
        if not raw.isdigit():
            fail(400, "编号不对")
        return reply({"deleted": await admin.queries.delete_memory(int(raw))})

    # ---- 群聊记录 ----

    async def chats(request: web.Request) -> web.Response:
        # days：只列最近这么多天有消息的群，条数、人数也只算这几天的；0 = 不限时间
        days = int_param(request, "days", CHAT_DAYS, 0, _MAX_DAYS)
        items = await labelled(await admin.queries.chats(days=days))
        return reply({"items": items, "days": days})

    async def chat_messages(request: web.Request) -> web.Response:
        chat_id = request.match_info["chat"]
        limit = int_param(request, "limit", 100, 1, 500)
        before = text_param(request, "before")
        items = await admin.queries.chat_messages(
            chat_id,
            limit=limit + 1,
            before=int(before) if before and before.isdigit() else None,
            search=text_param(request, "q"),
            addressed_only=flag_param(request, "addressed"),
            user_id=text_param(request, "user"),
        )
        more = len(items) > limit
        return reply({"items": await labelled(items[:limit]), "more": more})

    # ---- 备注名 ----

    async def labels(_request: web.Request) -> web.Response:
        return reply(await admin.queries.labels())

    async def put_label(request: web.Request) -> web.Response:
        body = await read_body(request)
        kind, target = str(body.get("kind") or ""), str(body.get("id") or "").strip()
        if kind not in {"chat", "user"} or not target:
            fail(400, "备注名要说明是给会话（chat）还是给成员（user）起的，以及它的标识")
        label = " ".join(str(body.get("label") or "").split())[:40]
        await admin.queries.set_label(kind, target, label)
        return reply({"kind": kind, "id": target, "label": label})

    return [
        web.get("/api/kb/summary", kb_summary),
        web.get("/api/kb/chunks", kb_chunks),
        web.get("/api/kb/chunks/{id:.+}", kb_chunk),
        web.post("/api/kb/search", kb_search),
        web.get("/api/kb/images", kb_images),
        web.post("/api/kb/image-search", kb_image_search),
        web.get("/api/sessions", sessions),
        web.delete("/api/sessions/{key:.+}/history", clear_history),
        web.delete("/api/sessions/{key:.+}/memories", clear_memories),
        web.get("/api/sessions/{key:.+}", session),
        web.delete("/api/memories/{id}", delete_memory),
        web.get("/api/chatlog/chats", chats),
        web.get("/api/chatlog/chats/{chat}/messages", chat_messages),
        web.get("/api/labels", labels),
        web.put("/api/labels", put_label),
    ]
