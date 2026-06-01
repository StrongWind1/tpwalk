"""Tests for tpwalk.scrape._forums -- ForumSource: OpenWrt Discourse JSON + Google site: dorks.

Tests use httpx.MockTransport so no real network requests are made. The handler
dispatches on URL host: forum.openwrt.org → Discourse JSON; www.google.com → SERP HTML or 429.

Per SCRP-14, D-14, D-15, D-18.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx

# --- Discourse JSON fixtures ---

_DISCOURSE_PAGE1_MORE = {
    "posts": [
        {
            "id": 98765,
            "blurb": "The GPL source is at https://static.tp-link.com/upload/gpl-code/2023/202312/20231201/GPL_ArcherAX55V1.tar.gz for this model",
            "topic_id": 11111,
            "post_number": 3,
        }
    ],
    "topics": [{"id": 11111, "title": "TP-Link AX55 OpenWrt support"}],
    "grouped_search_result": {
        "more_posts": True,
        "post_ids": [98765],
        "error": None,
    },
}

_DISCOURSE_PAGE2_FINAL = {
    "posts": [
        {
            "id": 99999,
            "blurb": "Also check https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz for older devices",
            "topic_id": 22222,
            "post_number": 1,
        }
    ],
    "topics": [{"id": 22222, "title": "C7 GPL"}],
    "grouped_search_result": {
        "more_posts": False,
        "post_ids": [99999],
        "error": None,
    },
}

_DISCOURSE_EMPTY = {
    "posts": [],
    "topics": [],
    "grouped_search_result": {
        "more_posts": False,
        "post_ids": [],
        "error": None,
    },
}

# --- Google SERP fixtures ---

_GOOGLE_SERP_WITH_URL = """<html><body>
<div class="yuRUbf">
  <a href="https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/GPL_AX55V1.tar.gz">
    <h3>GPL Source Code - TP-Link</h3>
  </a>
</div>
</body></html>"""

_GOOGLE_429_BODY = '{"error": "rateLimited"}'

_GOOGLE_CONSENT_HTML = """<html><body><p>Before you continue to Google</p></body></html>"""

_GOOGLE_UNUSUAL_TRAFFIC_HTML = """<html><body>
<p>Our systems have detected unusual traffic from your computer network.</p>
</body></html>"""


# --- Helpers ---


def _get_query_param(request_url: str, key: str) -> str:
    """Extract a single query parameter value from a URL."""
    parsed = urlparse(request_url)
    params = parse_qs(parsed.query)
    return params.get(key, [""])[0]


# --- Tests ---


def test_forum_source_name() -> None:
    """ForumSource.name returns 'forums' (D-15 txt filename stem)."""
    from tpwalk.scrape._forums import ForumSource

    source = ForumSource()
    assert source.name == "forums"


def test_openwrt_discourse_extracts_urls() -> None:
    """Mocked forum.openwrt.org/search.json with a post whose blurb contains a GPL URL: that URL is in the returned set (SCRP-14)."""
    from tpwalk.scrape._forums import ForumSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            # Single page, more_posts=False so source stops after page 1
            return httpx.Response(200, json=_DISCOURSE_PAGE2_FINAL)
        if "google.com" in url:
            # Return empty SERP to isolate Discourse path
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    assert "https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz" in result


def test_openwrt_discourse_pagination() -> None:
    """Page 1 has more_posts=True with one URL; page 2 has more_posts=False with another URL. Source fetches both pages and stops (SCRP-14 pagination)."""
    from tpwalk.scrape._forums import ForumSource

    page_responses: dict[str, dict] = {
        "1": _DISCOURSE_PAGE1_MORE,
        "2": _DISCOURSE_PAGE2_FINAL,
    }
    pages_fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            page = _get_query_param(url, "page")
            pages_fetched.append(page)
            response_data = page_responses.get(page, _DISCOURSE_EMPTY)
            return httpx.Response(200, json=response_data)
        if "google.com" in url:
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Both URLs from page 1 and page 2 should be present
    assert "https://static.tp-link.com/upload/gpl-code/2023/202312/20231201/GPL_ArcherAX55V1.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz" in result

    # Page 2 was fetched (pagination fired), page 3 was NOT fetched (more_posts=False stopped it)
    assert "2" in pages_fetched
    assert "3" not in pages_fetched


def test_discourse_pagination_page_ceiling() -> None:
    """Source caps at 4 pages max regardless of more_posts value (A4 assumption -- Discourse pagination unreliable past 3)."""
    from tpwalk.scrape._forums import ForumSource

    pages_fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            page = _get_query_param(url, "page")
            pages_fetched.append(page)
            # Always claim more_posts=True so source must rely on the page ceiling
            return httpx.Response(
                200,
                json={
                    "posts": [],
                    "topics": [],
                    "grouped_search_result": {"more_posts": True, "post_ids": [], "error": None},
                },
            )
        if "google.com" in url:
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    # Must not exceed page 4 (range(1, 5))
    numeric_pages = [int(p) for p in pages_fetched if p.isdigit()]
    assert max(numeric_pages) <= 4, f"Page ceiling exceeded: pages fetched = {pages_fetched}"
    assert "5" not in pages_fetched


def test_ddwrt_google_dork_extracts_urls() -> None:
    """Mocked google.com/search response for site:forum.dd-wrt.com query with a GPL URL href: URL is in the returned set (SCRP-14 dork path)."""
    from tpwalk.scrape._forums import ForumSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            return httpx.Response(200, json=_DISCOURSE_EMPTY)
        if "google.com" in url:
            q = _get_query_param(url, "q")
            if "forum.dd-wrt.com" in q:
                return httpx.Response(200, text=_GOOGLE_SERP_WITH_URL)
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    assert "https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/GPL_AX55V1.tar.gz" in result


def test_forum_source_skips_on_block() -> None:
    """429 from Google: source returns set() for the dork result, does not raise. OpenWrt results still flow through (SCRP-05, D-14)."""
    from tpwalk.scrape._forums import ForumSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            # Return one URL from Discourse path
            return httpx.Response(200, json=_DISCOURSE_PAGE2_FINAL)
        if "google.com" in url:
            # All Google requests return 429 (blocked)
            return httpx.Response(429, text=_GOOGLE_429_BODY)
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    # Must not raise
    result = asyncio.run(_run())

    # OpenWrt result still present despite Google block
    assert "https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz" in result


def test_forum_source_skips_on_consent_wall() -> None:
    """Google redirect to consent.google.com: source soft-skips without raising (Pitfall 6, D-14)."""
    from tpwalk.scrape._forums import ForumSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            return httpx.Response(200, json=_DISCOURSE_EMPTY)
        if "consent.google.com" in url:
            return httpx.Response(200, text=_GOOGLE_CONSENT_HTML)
        if "google.com" in url:
            # Redirect to consent page; httpx.MockTransport supports follow_redirects
            return httpx.Response(302, headers={"location": "https://consent.google.com/ml?continue=..."})
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    # Must not raise; result is empty (no Discourse or Google URLs)
    assert isinstance(result, set)


def test_forum_source_skips_on_unusual_traffic() -> None:
    """'unusual traffic' in Google response body: source soft-skips without raising (Pitfall 6)."""
    from tpwalk.scrape._forums import ForumSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            return httpx.Response(200, json=_DISCOURSE_EMPTY)
        if "google.com" in url:
            return httpx.Response(200, text=_GOOGLE_UNUSUAL_TRAFFIC_HTML)
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert isinstance(result, set)


def test_forum_source_all_three_dork_domains() -> None:
    """Source issues dork queries for all three blocked-forum domains: forum.dd-wrt.com, snbforums.com, linksysinfo.org (SCRP-14)."""
    from tpwalk.scrape._forums import ForumSource

    dork_queries_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            return httpx.Response(200, json=_DISCOURSE_EMPTY)
        if "google.com" in url:
            q = _get_query_param(url, "q")
            dork_queries_seen.append(q)
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    combined = " ".join(dork_queries_seen)
    assert "forum.dd-wrt.com" in combined, f"DD-WRT dork not issued: {dork_queries_seen}"
    assert "snbforums.com" in combined, f"SNBForums dork not issued: {dork_queries_seen}"
    assert "linksysinfo.org" in combined, f"FreshTomato (linksysinfo.org) dork not issued: {dork_queries_seen}"


def test_forum_source_mercusys_urls_from_discourse() -> None:
    """Discourse blurb containing a static.mercusys.com/gpl/ URL is also extracted (regex covers both brands)."""
    from tpwalk.scrape._forums import ForumSource

    mercusys_discourse = {
        "posts": [
            {
                "id": 55555,
                "blurb": "Mercusys GPL is at https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz",
                "topic_id": 33333,
                "post_number": 2,
            }
        ],
        "topics": [{"id": 33333, "title": "Mercusys GPL"}],
        "grouped_search_result": {"more_posts": False, "post_ids": [55555], "error": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            return httpx.Response(200, json=mercusys_discourse)
        if "google.com" in url:
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert "https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz" in result


def test_forum_source_discourse_none_response() -> None:
    """Discourse endpoint returns non-200: source does not raise, returns whatever Google found (SCRP-05 isolation)."""
    from tpwalk.scrape._forums import ForumSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            return httpx.Response(503, text="Service Unavailable")
        if "google.com" in url:
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert isinstance(result, set)


def test_forum_discourse_strips_trailing_punctuation() -> None:
    """WR-01 regression: trailing sentence punctuation is stripped from Discourse blurb URLs.

    A post blurb ending with "...file.tar.gz." must yield the clean URL without
    the trailing period in the result set. Verifies the rstrip(_TRAILING_PUNCT) fix.
    """
    from tpwalk.scrape._forums import ForumSource

    url_clean = "https://static.tp-link.com/resources/gpl/Blurb_Trail.tar.gz"
    discourse_with_trailing_dot = {
        "posts": [
            {
                "id": 77777,
                "blurb": f"See the GPL source at {url_clean}.",
                "topic_id": 44444,
                "post_number": 1,
            }
        ],
        "topics": [{"id": 44444, "title": "GPL test"}],
        "grouped_search_result": {"more_posts": False, "post_ids": [77777], "error": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "forum.openwrt.org" in url:
            return httpx.Response(200, json=discourse_with_trailing_dot)
        if "google.com" in url:
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(404)

    source = ForumSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert url_clean in result, f"Clean URL not found: {result}"
    assert f"{url_clean}." not in result, f"Trailing dot URL must not appear: {result}"


def test_forum_source_conforms_to_protocol() -> None:
    """ForumSource structurally conforms to ScrapeSource Protocol (D-17)."""
    from tpwalk.scrape import ScrapeSource
    from tpwalk.scrape._forums import ForumSource

    source = ForumSource()
    assert isinstance(source, ScrapeSource), "ForumSource does not conform to ScrapeSource Protocol"
