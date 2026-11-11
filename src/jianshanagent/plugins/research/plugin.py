"""research 插件：把查证工作流注册成服务（``research``）。

chat 插件发现有这个服务时，对话模型就只负责判断「要不要查」：游戏问题交给工作流，
工作流的结论直接发给用户。流程见 workflow.py，问题分析见 analysis.py，各级取证见 evidence.py，
配置在 plugin.research。

需要大模型和知识库检索（knowledge 插件，要有数据库）；缺任何一个都不注册，chat 插件退回老办法。
查别名要 wiki_query 插件，以图搜图要 knowledge 插件的图片索引，联网搜索要 core.websearch.yaml 打开；
没有的话对应的那一步自动不做。

提示词里因站而异的说法（这个 wiki 叫什么、问题分哪几类、名字都有哪些……）不写在代码里：
站名在 ``core.wiki.yaml``，其余的在 ``plugin.research`` 的 ``wording`` 和 ``analysis.topics``，
站点插件在自己配置的 ``extend`` 段里换成那个站的说法。
"""

from __future__ import annotations

import sys
from typing import Any
from zoneinfo import ZoneInfo

from jianshanagent.core.llm import LLMClient
from jianshanagent.core.logging import get_logger
from jianshanagent.core.plugin import PluginContext
from jianshanagent.core.websearch import WebSearchClient, WebSearchSettings
from jianshanagent.core.wiki import WikiSubject, wiki_subject
from jianshanagent.plugins.knowledge.retrieval import Retriever
from jianshanagent.plugins.research.analysis import DEFAULT_TOPICS, NameLookup, NameNote, Wording
from jianshanagent.plugins.research.evidence import (
    EntryPages,
    EvidenceSource,
    KnowledgeSource,
    PageFiller,
    SiteSearchSource,
    WebSearchSource,
    site_keywords_hint,
)
from jianshanagent.plugins.research.workflow import ResearchWorkflow, WorkflowSettings

logger = get_logger(__name__)

#: ``whole_page.mode`` 的三种取值：全量 / 节选 / 不补
WHOLE_FULL, WHOLE_EXCERPT, WHOLE_OFF = "full", "excerpt", "off"
WHOLE_MODES = (WHOLE_FULL, WHOLE_EXCERPT, WHOLE_OFF)


def wording_from(raw: dict[str, Any], subject: WikiSubject) -> Wording:
    """``plugin.research`` 的 ``wording`` 段 + ``core.wiki.yaml`` 里的站名 → 提示词里的说法。
    没写的项用对什么 wiki 都说得通的默认说法。"""
    defaults = Wording()

    def text(key: str, default: str) -> str:
        return str(raw.get(key) or "").strip() or default

    return Wording(
        bot=subject.bot,
        intro=subject.intro,
        names=text("names", defaults.names),
        generic_words=tuple(str(word) for word in raw.get("generic_words") or [] if str(word)),
        facts=text("facts", defaults.facts),
        mixups=text("mixups", defaults.mixups),
        analyst_note=text("analyst_note", ""),
        reviewer_note=text("reviewer_note", ""),
        keyword_nouns=text("keyword_nouns", ""),
        keyword_example=text("keyword_example", ""),
    )


def wiki_name_lookup(wiki: Any) -> NameLookup:
    """用 wiki 的数据查名字的别名（站点插件登记的昵称、页面的重定向）。"""

    async def lookup(name: str) -> list[NameNote]:
        return [
            NameNote(
                written=name,
                title=match.title,
                # 条目的种类给模型看的说法：站点插件认得的（式神、御魂……）用它的叫法，其余是页面
                kind=wiki.kind_label(match.kind),
                aliases=tuple(match.aliases),
                exact=bool(match.exact),
            )
            for match in await wiki.lookup_name(name)
        ]

    return lookup


class ResearchPlugin:
    id = "research"

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config()
        if not cfg.get("enabled", False):
            logger.info("research_plugin_disabled")
            return
        llm: LLMClient | None = ctx.services.get("llm")
        retriever: Retriever | None = ctx.services.get("knowledge")
        if llm is None or retriever is None:
            logger.warning("research_plugin_needs_llm_and_knowledge")
            return

        labels = cfg.get("labels") or {}
        profiles = cfg.get("profiles") or {}
        analysis = cfg.get("analysis") or {}
        images = cfg.get("images") or {}
        defaults = WorkflowSettings()
        subject = wiki_subject(ctx.config)
        wording = wording_from(cfg.get("wording") or {}, subject)
        rejections = cfg.get("max_rejections")
        retries = cfg.get("quick_retries")
        settings = WorkflowSettings(
            analysis_profile=str(profiles.get("analysis") or "")
            if analysis.get("enabled", True)
            else "",
            analyst_profile=str(profiles.get("analyst") or defaults.analyst_profile),
            reviewer_profile=str(profiles.get("reviewer") or defaults.reviewer_profile),
            quick_profile=str(profiles.get("quick") or ""),
            quick_timeout_seconds=float(
                cfg.get("quick_timeout_seconds") or defaults.quick_timeout_seconds
            ),
            quick_retries=int(retries if retries is not None else defaults.quick_retries),
            think_timeout_seconds=float(
                cfg.get("think_timeout_seconds") or defaults.think_timeout_seconds
            ),
            fast_profiles=frozenset(
                name for name, profile in llm.settings.profiles.items() if not profile.thinking
            ),
            deadline_seconds=float(cfg.get("deadline_seconds") or defaults.deadline_seconds),
            analysis_timeout_seconds=float(
                analysis.get("timeout_seconds") or defaults.analysis_timeout_seconds
            ),
            analysis_topics=tuple(str(t) for t in analysis.get("topics") or DEFAULT_TOPICS),
            wording=wording,
            max_names=int(analysis.get("max_names") or defaults.max_names),
            max_images=int(images.get("max_images") or defaults.max_images),
            image_label=str(labels.get("image") or defaults.image_label),
            max_rejections=int(rejections if rejections is not None else defaults.max_rejections),
            max_citations=int(cfg.get("max_citations") or defaults.max_citations),
            evidence_chars=int(cfg.get("evidence_chars") or defaults.evidence_chars),
            answer_chars=int(cfg.get("answer_chars") or defaults.answer_chars),
            max_seconds=float(cfg.get("max_seconds") or defaults.max_seconds),
            progress={
                str(stage): str(text) for stage, text in (cfg.get("progress") or {}).items() if text
            },
        )
        # 角色配的档位在 core.llm.yaml 里得有。不在这里拦下来的话，要等到有人提问、调用模型时才报错
        # （管理后台里改档位名字时最容易漏掉这边）
        wanted = (
            settings.analysis_profile,
            settings.analyst_profile,
            settings.reviewer_profile,
            settings.quick_profile,
        )
        unknown = sorted({name for name in wanted if name and name not in llm.settings.profiles})
        if unknown:
            raise ValueError(
                "plugin.research 的 profiles 里写了 core.llm.yaml 没有的档位：" + "、".join(unknown)
            )
        timezone = ZoneInfo(str(ctx.config.app.get("timezone") or "Asia/Shanghai"))

        available: dict[str, EvidenceSource] = {
            "kb": KnowledgeSource(retriever, label=str(labels.get("kb") or "知识库"))
        }
        wiki = ctx.services.get("wiki_query_client")  # wiki_query 插件启用时才有
        if wiki is not None:
            site = cfg.get("site") or {}
            fulltext = wiki.fulltext_titles

            async def search(keywords: str, limit: int) -> list[str]:
                titles: list[str] = await fulltext(keywords, limit=limit)
                return titles

            available["site"] = SiteSearchSource(
                retriever,
                search,
                label=str(labels.get("site") or "站内搜索"),
                pages=int(site.get("pages") or 5),
                chunks=int(site.get("chunks") or 6),
                timeout_seconds=float(site.get("timeout_seconds") or 5),
                keywords_hint=site_keywords_hint(wording.keyword_nouns, wording.keyword_example),
            )
        web_settings = WebSearchSettings.from_config(ctx.config)  # 没打开、没填密钥时是 None
        if web_settings is not None:
            web_search = WebSearchClient(web_settings)
            ctx.register_service("web_search", web_search)  # 登记成服务，退出时由宿主关掉连接
            available["web"] = WebSearchSource(
                web_search.search,
                label=str(labels.get("web") or "联网搜索"),
                # 搜索词里要带上的词：不另写就是这个 wiki 讲的东西的名字
                topic=str((cfg.get("web") or {}).get("topic") or subject.name),
            )

        # 每一级写一个来源，或者写一组一起查的来源；用不了的来源跳过，整级都用不了的级跳过
        stages = [
            [str(name) for name in stage] if isinstance(stage, list) else [str(stage)]
            for stage in (cfg.get("stages") or ["kb", "site", "web"])
        ]
        levels = [[available[name] for name in stage if name in available] for stage in stages]
        levels = [level for level in levels if level]
        if not levels:
            logger.warning("research_plugin_has_no_source", stages=stages)
            return
        image_search = ctx.services.get("image_search")  # knowledge 插件建了图片索引时才有
        whole = cfg.get("whole_page") or {}
        whole_mode = str(whole.get("mode") or WHOLE_EXCERPT)
        if whole_mode not in WHOLE_MODES:
            logger.warning("research_whole_page_mode_unknown", mode=whole_mode, using=WHOLE_EXCERPT)
            whole_mode = WHOLE_EXCERPT
        page_filler: PageFiller | None = None
        if whole_mode == WHOLE_FULL:
            # 全量：挑中的页面一律给整页，不看挑中了几段、页面有多长
            page_filler = PageFiller(
                retriever,
                label=str(labels.get("kb") or "知识库"),
                min_hits=1,
                max_chars=sys.maxsize,
            )
        elif whole_mode == WHOLE_EXCERPT:
            page_filler = PageFiller(
                retriever,
                label=str(labels.get("kb") or "知识库"),
                min_hits=int(whole.get("min_hits") or 2),
                max_chars=int(whole.get("max_chars") or 8000),
            )
        workflow = ResearchWorkflow(
            llm,
            levels,
            settings,
            timezone=timezone,
            name_lookup=wiki_name_lookup(wiki) if wiki is not None else None,
            entry_pages=EntryPages(
                retriever,
                label=str(labels.get("kb") or "知识库"),
                per_name=int(analysis.get("entry_chunks_per_name") or 2),
                total=int(analysis.get("entry_chunks") or 4),
            ),
            image_search=image_search,
            page_filler=page_filler,
        )
        ctx.register_service("research", workflow)
        logger.info(
            "research_plugin_enabled",
            stages=[[source.name for source in level] for level in levels],
            unavailable=sorted({name for stage in stages for name in stage} - set(available)),
            analysis=bool(settings.analysis_profile),
            name_lookup=wiki is not None,
            image_search=image_search is not None,
            whole_page=whole_mode,
            quick=bool(settings.quick_profile),
        )


def create_plugin() -> ResearchPlugin:
    return ResearchPlugin()
