"""wiki_query 插件：精确查询，不过大模型。

``/查 <名字>`` 查 wiki 条目（有多个候选时给按钮点选）。另把同样的查询注册成给对话模型用的工具
（wiki_search / wiki_get），并把 wiki 查询客户端注册成服务供别的插件用。

查哪个 wiki 在 ``core.wiki.yaml``。通用的部分只认页面：按标题找、全文搜索、取摘要。
那个站另有自己的条目和命令的，由站点插件接进来（``core.wiki.yaml`` 的 ``site``，
接口见 provider.py）：
阴阳师 wiki 的式神、御魂（认昵称）和 ``/筛选``、wiki_filter 就是 ``yys`` 插件加的。

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
from jianshanagent.core.wiki import WikiSite, load_wiki_site
from jianshanagent.plugins.wiki_query.client import (
    KIND_PAGE,
    PageContent,
    WikiError,
    WikiQueryClient,
    provider_kinds,
)
from jianshanagent.plugins.wiki_query.formatters import (
    format_disambiguation,
    format_not_found,
    format_page,
    knowledge_extract,
)
from jianshanagent.plugins.wiki_query.provider import ProviderContext, WikiProvider

logger = get_logger(__name__)

_UNAVAILABLE = "wiki 暂时访问不了，请稍后再试。"

#: 取一个条目的内容：（名称，种类）→ 内容，没有就是 None
GetPage = Callable[[str, str | None], Awaitable[PageContent | None]]


def load_providers(ctx: PluginContext, wiki: WikiSite) -> list[WikiProvider]:
    """用到的各级站点插件里接了查询这个扩展点的，从上到下。"""
    providers: list[WikiProvider] = load_sites(ctx.config, EXTENSION_WIKI, wiki)
    return providers


class WikiQueryPlugin:
    id = "wiki_query"

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config()
        options = cfg.get("wiki") or {}
        # 是哪个 wiki 在 core.wiki.yaml；请求头名在那里，密钥在 secrets.yaml（不入库）
        wiki = load_wiki_site(ctx.config)
        if options.get("user_agent"):
            wiki = dataclasses.replace(wiki, user_agent=str(options["user_agent"]))
        if wiki.auth_header and not wiki.auth_key:
            logger.warning("wiki_authkey_missing", header=wiki.auth_header)

        client = WikiQueryClient(
            wiki,
            timeout_seconds=float(options.get("timeout_seconds") or 12),
            cache_ttl_seconds=float(options.get("cache_ttl_seconds") or 300),
            search_ranking=str(options.get("search_ranking") or ""),
        )
        providers = load_providers(ctx, wiki)
        for provider in providers:
            client.add_provider(provider)
            provider.bind(client)
        ctx.register_service("wiki_query_client", client)

        fmt = cfg.get("format") or {}
        max_chars = int(fmt.get("max_chars") or 800)
        include_link = bool(fmt.get("include_link", True))
        disambiguation_limit = int(fmt.get("disambiguation_limit") or 5)

        commands: dict[str, dict[str, Any]] = cfg.get("commands") or {}
        lookup_cfg = commands.get("lookup") or {"name": "查"}
        lookup_name = str(lookup_cfg.get("name") or "查")
        # 「/查 <式神、御魂或条目名>」：站点插件认得哪几类条目，就说哪几类
        kind_labels = client.kind_labels
        what = "、".join(kind_labels.values()) + "或条目名" if kind_labels else "条目名"

        markdown = bool(fmt.get("markdown", False))
        use_buttons = bool(fmt.get("buttons", False))
        knowledge_fallback = bool(fmt.get("knowledge_fallback", False))
        services = ctx.services

        async def get_page(title: str, kind: str | None) -> PageContent | None:
            """取条目内容；普通页面的摘要是空的时候，用知识库里这一页的正文顶上。"""
            page = await client.get_page(title, kind=kind)
            if page is None or page.kind != KIND_PAGE or page.extract or not knowledge_fallback:
                return page
            # knowledge 插件开着才有；它在本插件之后注册，所以用的时候才取
            knowledge: Any = services.get("knowledge")
            if knowledge is None:
                return page
            try:
                text = await knowledge.page_text(
                    page.title, chars=max_chars, url_prefix=wiki.page_url_prefix
                )
            except Exception as exc:  # 知识库出问题不该让 /查 用不了：照旧只给标题和链接
                logger.warning("wiki_page_knowledge_failed", error=str(exc)[:200])
                return page
            page.extract = knowledge_extract(text)
            return page

        def reply(message: InboundMessage, text: str) -> OutboundMessage:
            return OutboundMessage(content=text, reply_to=message.message_id)

        async def handle_lookup(message: InboundMessage, args: str) -> OutboundMessage | None:
            query = (args or "").strip()
            if not query:
                return reply(message, f"用法：/{lookup_name} <{what}>")
            try:
                hits = await client.search(query, limit=disambiguation_limit)
                # 名字完全一致的直接展示（不同种类的条目重名时都给）；否则只有一个候选才展示
                chosen = [h for h in hits if h.exact] or (hits if len(hits) == 1 else [])
                if hits and not chosen:
                    out = reply(
                        message,
                        format_disambiguation(
                            query,
                            hits,
                            command=lookup_name,
                            buttons=use_buttons,
                            kind_labels=kind_labels,
                        ),
                    )
                    if use_buttons:
                        out.buttons = [Button(h.title, f"/{lookup_name} {h.title}") for h in hits]
                    return out
                pages = [await get_page(h.title, h.kind) for h in chosen]
            except WikiError:
                logger.exception("wiki_lookup_failed", query_chars=len(query))
                return reply(message, _UNAVAILABLE)

            found = [page for page in pages if page is not None]
            if not found:
                return reply(message, format_not_found(query))
            budget = max_chars // len(found)
            text = "\n\n".join(
                format_page(
                    page,
                    max_chars=budget,
                    include_link=include_link,
                    markdown=markdown,
                    providers=providers,
                )
                for page in found
            )
            out = reply(message, text)
            out.markdown = markdown
            return out

        self._register_tools(ctx, client, get_page)

        ctx.add_command(
            lookup_name,
            handle_lookup,
            aliases=list(lookup_cfg.get("aliases") or []),
            description=str(lookup_cfg.get("description") or "查询 wiki 条目"),
        )
        # 站点插件自己的命令和工具（阴阳师的 /筛选）
        provider_ctx = ProviderContext(
            plugin=ctx, client=client, max_chars=max_chars, unavailable=_UNAVAILABLE
        )
        for provider in providers:
            provider.register(provider_ctx)

    @staticmethod
    def _register_tools(ctx: PluginContext, client: WikiQueryClient, get_page: GetPage) -> None:
        """把查询方法暴露给 LLM。参数来自模型，所以每个都做类型兜底。"""
        providers = client.providers
        kinds = [*provider_kinds(providers), KIND_PAGE]
        labels = list(client.kind_labels.values())

        async def tool_search(args: dict[str, Any], _message: InboundMessage) -> Any:
            hits = await client.search(str(args.get("query") or ""), limit=8)
            return [{"title": h.title, "kind": h.kind, "exact": h.exact} for h in hits]

        async def tool_get(args: dict[str, Any], _message: InboundMessage) -> Any:
            kind = args.get("kind")
            page = await get_page(str(args.get("title") or ""), kind if kind in kinds else None)
            if page is None:
                return {"found": False}
            return {
                "found": True,
                "title": page.title,
                "kind": page.kind,
                "url": page.url,
                **({"extract": page.extract} if page.extract else {}),
                **page.data,
            }

        search_hint = "".join(p.search_hint for p in providers) or "支持页面标题和关键词。"
        where = client.wiki.label if client.wiki.name else " wiki"
        ctx.add_tool(
            "wiki_search",
            f"在{where} 里按名称找条目。{search_hint}返回候选列表，exact=true 表示名称完全一致。",
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
        ctx.add_tool(
            "wiki_get",
            "取一个条目的内容。"
            + "".join(p.get_hint for p in providers)
            + "普通页面返回正文摘要。title 用 wiki_search 返回的 title。",
            {"type": "object", "properties": properties, "required": ["title"]},
            tool_get,
        )


def create_plugin() -> WikiQueryPlugin:
    return WikiQueryPlugin()
