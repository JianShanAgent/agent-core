from __future__ import annotations

import httpx
import pytest

from jianshanagent.plugins.knowledge.fetcher import WikiFetcher
from jianshanagent.plugins.knowledge.settings import FetchSettings
from jianshanagent.core.wiki import WikiSite


@pytest.mark.asyncio
async def test_fetcher_sends_site_referer_and_cookie() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({key.lower(): value for key, value in request.headers.items()})
        return httpx.Response(200, json={"ok": True})

    wiki = WikiSite(
        id="bwiki",
        api_url="https://wiki.biligame.com/yys/api.php",
        site_base="https://wiki.biligame.com/yys",
        cookie="SESSDATA=secret",
    )
    fetcher = WikiFetcher(
        wiki,
        FetchSettings(max_retries=0),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await fetcher.api("test", {"action": "query"}) == {"ok": True}
    finally:
        await fetcher.aclose()

    assert seen["referer"] == "https://wiki.biligame.com/yys/"
    assert seen["cookie"] == "SESSDATA=secret"
