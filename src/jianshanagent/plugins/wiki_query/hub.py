"""挂着的各个 wiki 的查询客户端合在一起：命令、LLM 工具和查证工作流用的是这一层。

一个部署可以挂好几个 wiki（``core.wiki.yaml`` 的 ``wikis``），每个 wiki 有自己的
:class:`~jianshanagent.plugins.wiki_query.client.WikiQueryClient`（各带各的站点插件）。
这里把同一个问题挨个问过去，把结果合起来；查到的每样东西上都标着出自哪个 wiki
（``wiki`` 字段，是 ``core.wiki.yaml`` 里的 id）。只挂了一个 wiki 时，结果和直接问那个客户端一样。

某个 wiki 一时访问不了，不影响别的：能答的照答，日志里记一笔；全都访问不了才报错。
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Sequence
from itertools import chain, zip_longest
from typing import TypeVar

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.wiki_query.client import (
    NameMatch,
    PageContent,
    SearchHit,
    WikiError,
    WikiQueryClient,
)
from jianshanagent.plugins.wiki_query.provider import WikiProvider

logger = get_logger(__name__)

T = TypeVar("T")


class WikiQueryHub:
    def __init__(self, clients: Sequence[WikiQueryClient]):
        if not clients:
            raise ValueError("至少要有一个 wiki 的查询客户端")
        #: 各个 wiki 的客户端，按 ``core.wiki.yaml`` 里的顺序（排第一的是主 wiki）
        self.clients: tuple[WikiQueryClient, ...] = tuple(clients)

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()

    # ── 挂着哪些 wiki ─────────────────────────────────────────────────

    @property
    def multiple(self) -> bool:
        """挂了不止一个 wiki：这时回答里才需要标出处。"""
        return len(self.clients) > 1

    def client(self, wiki: str) -> WikiQueryClient | None:
        """``wiki`` 这个 wiki 的客户端；没有这个 wiki 返回 None。"""
        return next((client for client in self.clients if client.wiki.id == wiki), None)

    def wiki_label(self, wiki: str) -> str:
        """出处里怎么称呼 ``wiki``（``core.wiki.yaml`` 里那个 wiki 的 ``label``）。"""
        client = self.client(wiki)
        return (client.wiki.label or client.wiki.id) if client is not None else wiki

    @property
    def providers(self) -> list[WikiProvider]:
        """各个 wiki 的站点插件，按 wiki 的顺序。"""
        return [provider for client in self.clients for provider in client.providers]

    @property
    def kind_labels(self) -> dict[str, str]:
        """各个 wiki 的站点插件认得的条目种类：代号 → 给人看的名字。"""
        labels: dict[str, str] = {}
        for client in self.clients:
            for kind, label in client.kind_labels.items():
                labels.setdefault(kind, label)
        return labels

    def kind_label(self, kind: str, default: str = "页面", *, wiki: str = "") -> str:
        """条目种类给人看的名字。``wiki``：这个种类是哪个 wiki 的（代号各 wiki 自己起，可能重）。"""
        client = self.client(wiki) if wiki else None
        labels = client.kind_labels if client is not None else self.kind_labels
        return labels.get(kind, default)

    # ── 查询 ──────────────────────────────────────────────────────────

    async def _each(self, what: str, calls: Sequence[Awaitable[T]]) -> list[T | None]:
        """各个 wiki 一起问。访问不了的那个当作没结果（None）；全都访问不了，把错误抛出去。"""
        results = await asyncio.gather(*calls, return_exceptions=True)
        answers: list[T | None] = []
        failures: list[WikiError] = []
        for result in results:
            if isinstance(result, WikiError):
                failures.append(result)
                answers.append(None)
            elif isinstance(result, BaseException):
                raise result
            else:
                answers.append(result)
        if failures and len(failures) == len(self.clients):
            raise failures[0]
        for client, answer in zip(self.clients, answers, strict=True):
            if answer is None:
                logger.warning("wiki_unreachable", wiki=client.wiki.id, what=what)
        return answers

    async def search(self, q: str, *, limit: int = 5) -> list[SearchHit]:
        """按名字找条目，各个 wiki 都找。

        名字完全一致的排前面（每个 wiki 的都在）；其余的各个 wiki 轮着排，一共最多 ``limit`` 个。
        """
        found = await self._each("search", [c.search(q, limit=limit) for c in self.clients])
        per_wiki = [hits or [] for hits in found]
        exact = [hit for hits in per_wiki for hit in hits if hit.exact]
        others = [[hit for hit in hits if not hit.exact] for hits in per_wiki]
        mixed = [hit for hit in chain.from_iterable(zip_longest(*others)) if hit is not None]
        return (exact + mixed)[: max(limit, len(exact))]

    async def lookup_name(self, name: str, *, candidates: int = 3) -> list[NameMatch]:
        """查一个名字（可能是外号）对应哪个条目。各个 wiki 都查，说的是同一个条目的并成一条。

        有 wiki 认得这个名字（``exact``）时，别的 wiki 给的「名字里包含它」的候选就不要了。
        """
        found = await self._each(
            "lookup_name", [c.lookup_name(name, candidates=candidates) for c in self.clients]
        )
        matches = [match for matches in found for match in matches or []]
        if any(match.exact for match in matches):
            matches = [match for match in matches if match.exact]
        merged: dict[tuple[str, str], NameMatch] = {}
        for match in matches:
            # 同名、同一类的条目（两个 wiki 上都有「山风」这个式神）是同一个东西
            key = (match.title, self.kind_label(match.kind, wiki=match.wiki))
            known = merged.get(key)
            if known is None:
                merged[key] = match
            else:
                aliases = tuple(dict.fromkeys((*known.aliases, *match.aliases)))
                merged[key] = dataclasses.replace(known, aliases=aliases)
        result = list(merged.values())
        return result if any(match.exact for match in result) else result[:candidates]

    async def fulltext_titles(self, keywords: str, *, limit: int = 5) -> dict[str, list[str]]:
        """站内全文搜索，各个 wiki 各搜一次：wiki 的 id → 命中的页面标题（按站内的排序）。"""
        found = await self._each(
            "fulltext", [c.fulltext_titles(keywords, limit=limit) for c in self.clients]
        )
        return {
            client.wiki.id: titles
            for client, titles in zip(self.clients, found, strict=True)
            if titles
        }

    async def get_page(
        self, title: str, *, kind: str | None = None, wiki: str = ""
    ) -> PageContent | None:
        """取条目内容。``wiki``、``kind`` 来自 :class:`SearchHit`。

        不说是哪个 wiki 的，就按顺序挨个问，给第一个有这个条目的。
        """
        chosen = self.client(wiki) if wiki else None
        clients = [chosen] if chosen is not None else list(self.clients)
        failure: WikiError | None = None
        for client in clients:
            if kind is not None and kind not in client.kind_labels and len(clients) > 1:
                # 这个种类是别的 wiki 的站点插件起的代号
                if any(kind in other.kind_labels for other in clients):
                    continue
            try:
                page = await client.get_page(title, kind=kind)
            except WikiError as exc:
                failure = failure or exc
                logger.warning("wiki_unreachable", wiki=client.wiki.id, what="get_page")
                continue
            if page is not None:
                return page
        if failure is not None:
            raise failure
        return None
