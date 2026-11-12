"""wiki_query 插件：精确查询，不过大模型。

``/查 <名字>`` 查 wiki 条目（有多个候选时给按钮点选）。另把同样的查询注册成给对话模型用的工具
（wiki_search / wiki_get），并把查询注册成服务（``wiki_query``，见 hub.py）供别的插件用。

查哪些 wiki 在 ``core.wiki.yaml`` 的 ``wikis``。通用的部分只认页面：按标题找、全文搜索、取摘要。
那个站另有自己的条目和命令的，由站点插件接进来（那个 wiki 的 ``site``，接口见 provider.py）：
灰机阴阳师 wiki 的式神、御魂（认昵称）和 ``/筛选``、wiki_filter 就是 ``huiji_yys`` 插件加的。

挂了不止一个 wiki 时，每个 wiki 各查各的：名字完全一致的条目各个 wiki 的都给，标明出自哪个 wiki。
请求怎么发（``wiki`` 段：超时、缓存、搜索的排法）各个 wiki 读自己那一份配置，站点插件可以各补各的；
命令叫什么、结果怎么排（``commands``、``format``）只有一份，用主 wiki 的。

普通页面的正文取的是 wiki 接口的摘要。模板拼出来的页面摘要是空的，
这时用知识库里这一页的正文顶上（``format.knowledge_fallback``，要 knowledge 插件开着）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import Button, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext
from jianshanagent.core.sites import EXTENSION_WIKI, load_sites
from jianshanagent.core.wiki import WikiSite, load_wikis, wiki_subject
from jianshanagent.plugins.wiki_query.client import (
    KIND_PAGE,
    PageContent,
    SearchHit,
    WikiError,
    WikiQueryClient,
)
from jianshanagent.plugins.wiki_query.formatters import (
    format_disambiguation,
    format_not_found,
    format_page,
    format_source,
    knowledge_extract,
)
from jianshanagent.plugins.wiki_query.hub import WikiQueryHub
from jianshanagent.plugins.wiki_query.provider import ProviderContext, WikiProvider

logger = get_logger(__name__)

_UNAVAILABLE = "wiki 暂时访问不了，请稍后再试。"

#: 取一个条目的内容：（名称，种类，哪个 wiki）→ 内容，没有就是 None
GetPage = Callable[[str, str | None, str], Awaitable[PageContent | None]]


def load_providers(ctx: PluginContext, wiki: WikiSite) -> list[WikiProvider]:
    """``wiki`` 用到的各级站点插件里接了查询这个扩展点的，从上到下。"""
    providers: list[WikiProvider] = load_sites(ctx.config, EXTENSION_WIKI, wiki)
    return providers


def build_client(ctx: PluginContext, wiki: WikiSite) -> WikiQueryClient:
    """``wiki`` 的查询客户端，带上它的站点插件。请求怎么发读的是这个 wiki 的那一份配置。"""
    options = ctx.config.for_wiki(wiki.id).plugin(WikiQueryPlugin.id).get("wiki") or {}
    if options.get("user_agent"):
        wiki = dataclasses.replace(wiki, user_agent=str(options["user_agent"]))
    if wiki.auth_header and not wiki.auth_key:
        logger.warning("wiki_authkey_missing", wiki=wiki.id, header=wiki.auth_header)
    client = WikiQueryClient(
        wiki,
        timeout_seconds=float(options.get("timeout_seconds") or 12),
        cache_ttl_seconds=float(options.get("cache_ttl_seconds") or 300),
        search_ranking=str(options.get("search_ranking") or ""),
    )
    for provider in load_providers(ctx, wiki):
        client.add_provider(provider)
        provider.bind(client)
    return client


class WikiQueryPlugin:
    id = "wiki_query"

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config()
        # 挂着哪些 wiki 在 core.wiki.yaml；请求头名在那里，密钥在 secrets.yaml（不入库）
        hub = WikiQueryHub([build_client(ctx, wiki) for wiki in load_wikis(ctx.config)])
        ctx.register_service("wiki_query", hub)

        fmt = cfg.get("format") or {}
        max_chars = int(fmt.get("max_chars") or 800)
        include_link = bool(fmt.get("include_link", True))
        disambiguation_limit = int(fmt.get("disambiguation_limit") or 5)

        commands: dict[str, dict[str, Any]] = cfg.get("commands") or {}
        lookup_cfg = commands.get("lookup") or {"name": "查"}
        lookup_name = str(lookup_cfg.get("name") or "查")
        # 「/查 <式神、御魂或条目名>」：站点插件认得哪几类条目，就说哪几类
        kind_labels = hub.kind_labels
        kind_names = list(dict.fromkeys(kind_labels.values()))
        what = "、".join(kind_names) + "或条目名" if kind_names else "条目名"

        markdown = bool(fmt.get("markdown", False))
        use_buttons = bool(fmt.get("buttons", False))
        knowledge_fallback = bool(fmt.get("knowledge_fallback", False))
        services = ctx.services

        async def get_page(title: str, kind: str | None, wiki: str = "") -> PageContent | None:
            """取条目内容；普通页面的摘要是空的时候，用知识库里这一页的正文顶上。"""
            page = await hub.get_page(title, kind=kind, wiki=wiki)
            if page is None or page.kind != KIND_PAGE or page.extract or not knowledge_fallback:
                return page
            # knowledge 插件开着才有；它在本插件之后注册，所以用的时候才取
            knowledge: Any = services.get("knowledge")
            source = hub.client(page.wiki)
            if knowledge is None or source is None:
                return page
            try:
                text = await knowledge.page_text(
                    page.title, chars=max_chars, url_prefix=source.wiki.page_url_prefix
                )
            except Exception as exc:  # 知识库出问题不该让 /查 用不了：照旧只给标题和链接
                logger.warning("wiki_page_knowledge_failed", error=str(exc)[:200])
                return page
            page.extract = knowledge_extract(text)
            return page

        async def get_pages(hits: list[SearchHit]) -> list[PageContent]:
            """把候选的内容都取回来。某个 wiki 一时访问不了，别的照给；一个都取不到才算失败。"""
            pages: list[PageContent] = []
            failure: WikiError | None = None
            for hit in hits:
                try:
                    page = await get_page(hit.title, hit.kind, hit.wiki)
                except WikiError as exc:
                    failure = failure or exc
                    continue
                if page is not None:
                    pages.append(page)
            if failure is not None and not pages:
                raise failure
            return pages

        def render(pages: list[PageContent]) -> str:
            """一个 wiki 的条目排在一起、共用 ``max_chars``；挂了不止一个 wiki 时标明出处。"""
            blocks: list[str] = []
            for client in hub.clients:
                mine = [page for page in pages if page.wiki == client.wiki.id]
                if not mine:
                    continue
                source = format_source(client.wiki.label, markdown) if hub.multiple else ""
                budget = (max_chars - len(source)) // len(mine)
                texts = [
                    format_page(
                        page,
                        max_chars=budget,
                        include_link=include_link,
                        markdown=markdown,
                        providers=client.providers,
                    )
                    for page in mine
                ]
                blocks.append("\n\n".join([source, *texts] if source else texts))
            return "\n\n".join(blocks)

        def reply(message: InboundMessage, text: str) -> OutboundMessage:
            return OutboundMessage(content=text, reply_to=message.message_id)

        async def handle_lookup(message: InboundMessage, args: str) -> OutboundMessage | None:
            query = (args or "").strip()
            if not query:
                return reply(message, f"用法：/{lookup_name} <{what}>")
            try:
                hits = await hub.search(query, limit=disambiguation_limit)
                # 几个 wiki 上同名、同一类的候选算一个：列出来只列一次，展示时各个 wiki 的都给
                listed: dict[tuple[str, str], SearchHit] = {}
                for hit in hits:
                    listed.setdefault((hit.title, kind_labels.get(hit.kind, "")), hit)
                # 名字完全一致的直接展示（不同种类的条目重名时都给）；否则只有一个候选才展示
                chosen = [h for h in hits if h.exact] or (hits if len(listed) == 1 else [])
                if hits and not chosen:
                    out = reply(
                        message,
                        format_disambiguation(
                            query,
                            list(listed.values()),
                            command=lookup_name,
                            buttons=use_buttons,
                            kind_labels=kind_labels,
                        ),
                    )
                    if use_buttons:
                        titles = dict.fromkeys(title for title, _ in listed)
                        out.buttons = [Button(t, f"/{lookup_name} {t}") for t in titles]
                    return out
                found = await get_pages(chosen)
            except WikiError:
                logger.exception("wiki_lookup_failed", query_chars=len(query))
                return reply(message, _UNAVAILABLE)

            if not found:
                return reply(message, format_not_found(query))
            out = reply(message, render(found))
            out.markdown = markdown
            return out

        self._register_tools(ctx, hub, get_page)

        ctx.add_command(
            lookup_name,
            handle_lookup,
            aliases=list(lookup_cfg.get("aliases") or []),
            description=str(lookup_cfg.get("description") or "查询 wiki 条目"),
        )
        # 站点插件自己的命令和工具（灰机阴阳师 wiki 的 /筛选）。两个站点插件加了同名的命令或工具，
        # 宿主会在这里报冲突
        for client in hub.clients:
            provider_ctx = ProviderContext(
                plugin=ctx, client=client, max_chars=max_chars, unavailable=_UNAVAILABLE
            )
            for provider in client.providers:
                provider.register(provider_ctx)

    @staticmethod
    def _register_tools(ctx: PluginContext, hub: WikiQueryHub, get_page: GetPage) -> None:
        """把查询方法暴露给 LLM。参数来自模型，所以每个都做类型兜底。"""
        providers = hub.providers
        kinds = [*hub.kind_labels, KIND_PAGE]
        labels = list(dict.fromkeys(hub.kind_labels.values()))
        wikis = {client.wiki.id: client.wiki.label for client in hub.clients}

        def source(wiki: str) -> dict[str, str]:
            """挂了不止一个 wiki 时，结果里带上出自哪个 wiki。"""
            return {"wiki": wiki} if hub.multiple and wiki else {}

        async def tool_search(args: dict[str, Any], _message: InboundMessage) -> Any:
            hits = await hub.search(str(args.get("query") or ""), limit=8)
            return [
                {"title": h.title, "kind": h.kind, "exact": h.exact, **source(h.wiki)} for h in hits
            ]

        async def tool_get(args: dict[str, Any], _message: InboundMessage) -> Any:
            kind = args.get("kind")
            wiki = str(args.get("wiki") or "")
            page = await get_page(
                str(args.get("title") or ""),
                kind if kind in kinds else None,
                wiki if wiki in wikis else "",
            )
            if page is None:
                return {"found": False}
            return {
                "found": True,
                "title": page.title,
                "kind": page.kind,
                **source(page.wiki),
                "url": page.url,
                **({"extract": page.extract} if page.extract else {}),
                **page.data,
            }

        search_hint = "".join(dict.fromkeys(p.search_hint for p in providers))
        search_hint = search_hint or "支持页面标题和关键词。"
        subject = wiki_subject(ctx.config)
        where = subject.label if subject.name else " wiki"
        several = ""
        if hub.multiple:
            names = "、".join(f"{wiki}（{label}）" for wiki, label in wikis.items())
            several = f"资料来自几个 wiki：{names}，结果里的 wiki 表示出自哪一个。"
        ctx.add_tool(
            "wiki_search",
            f"在{where} 里按名称找条目。{search_hint}{several}"
            "返回候选列表，exact=true 表示名称完全一致。",
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "名称或关键词"}},
                "required": ["query"],
            },
            tool_search,
        )
        properties: dict[str, Any] = {"title": {"type": "string", "description": "条目的完整名称"}}
        if labels:
            # 站点插件认得几类条目时，同名的靠 kind 区分
            same_name = f"；{'与'.join(labels)}重名时用它区分" if len(labels) > 1 else ""
            properties["kind"] = {
                "type": "string",
                "enum": kinds,
                "description": f"wiki_search 返回的 kind{same_name}",
            }
        if hub.multiple:
            properties["wiki"] = {
                "type": "string",
                "enum": list(wikis),
                "description": "wiki_search 返回的 wiki：取哪个 wiki 上的这个条目；"
                "不写就给第一个有它的",
            }
        ctx.add_tool(
            "wiki_get",
            "取一个条目的内容。"
            + "".join(dict.fromkeys(p.get_hint for p in providers))
            + "普通页面返回正文摘要。title 用 wiki_search 返回的 title。",
            {"type": "object", "properties": properties, "required": ["title"]},
            tool_get,
        )


def create_plugin() -> WikiQueryPlugin:
    return WikiQueryPlugin()
