"""这个机器人服务的是哪个 wiki：地址、叫什么。

读 ``core.wiki.yaml``，查询、知识库、提示词都从这里拿。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from jianshanagent.core.config import WIKI_FILE, AppConfig

#: 页面地址在站内的默认写法：``<站点>/wiki/<标题>``
DEFAULT_ARTICLE_PATH = "/wiki/$1"
DEFAULT_USER_AGENT = "JianShanAgent/0.1"


@dataclass(frozen=True, slots=True)
class WikiSubject:
    """这个 wiki 讲的是什么，提示词里怎么称呼它。"""

    #: 「阴阳师」；没写就是空的
    name: str = ""
    #: 一句话说明它是什么（「网易手游」）；只在要向模型交代背景的地方用
    about: str = ""

    @property
    def label(self) -> str:
        """「阴阳师 wiki」；没写名字就是「wiki」。"""
        return f"{self.name} wiki" if self.name else "wiki"

    @property
    def intro(self) -> str:
        """接在「你在帮」后面、交代背景时的说法：「阴阳师（网易手游）wiki」。"""
        if not self.name:
            return " wiki"
        return f"{self.name}（{self.about}）wiki" if self.about else self.label

    @property
    def bot(self) -> str:
        """接在「你是」后面的自称：「阴阳师 wiki 机器人」。"""
        return f"{self.name} wiki 机器人" if self.name else " wiki 机器人"


def wiki_subject(config: AppConfig) -> WikiSubject:
    """``core.wiki.yaml`` 里写的名字和说明（没配地址也能读）。"""
    raw = config.get(WIKI_FILE)
    return WikiSubject(str(raw.get("name") or "").strip(), str(raw.get("about") or "").strip())


@dataclass(frozen=True, slots=True)
class WikiSite:
    api_url: str
    site_base: str
    #: 这个 wiki 讲的是什么（「阴阳师」）；没写就是空的
    name: str = ""
    #: 一句话说明它是什么（「网易手游」）；只在要向模型交代背景的地方用
    about: str = ""
    #: 页面地址在站内的写法（MediaWiki 的 ``$wgArticlePath``），``$1`` 是标题
    article_path: str = DEFAULT_ARTICLE_PATH
    user_agent: str = DEFAULT_USER_AGENT
    #: 放行请求头的名字和密钥（站点在 Cloudflare 后面时用）；密钥在 secrets.yaml
    auth_header: str | None = None
    auth_key: str | None = None
    cookie: str | None = None

    @property
    def label(self) -> str:
        """提示词里怎么称呼它：「阴阳师 wiki」；没写名字就是「wiki」。"""
        return WikiSubject(self.name, self.about).label

    def page_url(self, title: str) -> str:
        """一个页面的地址。"""
        name = quote(title.replace(" ", "_"), safe=":/")
        return f"{self.site_base}{self.article_path.replace('$1', name)}"

    @property
    def page_url_prefix(self) -> str:
        """这个站的页面地址都以什么开头（``https://…/wiki/``）。"""
        return f"{self.site_base}{self.article_path.split('$1', 1)[0]}"

    @property
    def link_prefix(self) -> str:
        """渲染结果里指向站内页面的链接以什么开头（``/wiki/``）。"""
        return urlsplit(self.site_base).path.rstrip("/") + self.article_path.split("$1", 1)[0]


def load_wiki_site(config: AppConfig) -> WikiSite:
    """读 ``core.wiki.yaml``；没写是哪个 wiki 就报错，不猜。"""
    raw = config.get(WIKI_FILE)
    secrets = config.secrets.get("wiki") or {}
    api_url = str(raw.get("api_url") or "")
    site_base = str(raw.get("site_base") or "").rstrip("/")
    if not site_base and api_url:
        parts = urlsplit(api_url)  # https://example.org/w/api.php → https://example.org
        site_base = f"{parts.scheme}://{parts.netloc}"
    if not site_base:
        raise ValueError(
            "不知道是哪个 wiki：core.wiki.yaml 里要写 api_url（接口地址）；"
            "页面地址不是 <站点>/wiki/<标题> 的，另写 site_base、article_path"
        )
    article_path = str(raw.get("article_path") or DEFAULT_ARTICLE_PATH)
    if "$1" not in article_path:
        raise ValueError(
            f"core.wiki.yaml 的 article_path 里要有 $1（标题的位置），现在是「{article_path}」"
        )
    return WikiSite(
        api_url=api_url or f"{site_base}/api.php",
        site_base=site_base,
        name=str(raw.get("name") or "").strip(),
        about=str(raw.get("about") or "").strip(),
        article_path=article_path,
        user_agent=str(raw.get("user_agent") or DEFAULT_USER_AGENT),
        auth_header=str(raw.get("auth_header") or "") or None,
        auth_key=str(secrets.get("authkey") or "") or None,
        cookie=str(secrets.get("cookie") or "") or None,
    )
