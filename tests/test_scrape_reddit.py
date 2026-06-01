"""Tests for tpwalk.scrape._reddit.RedditSource.

All HTTP is mocked via httpx.MockTransport — no real network calls.
Testing SCRP-15: Reddit public JSON search with after-cursor pagination,
per-subreddit and site-wide queries, and User-Agent enforcement.
"""

from __future__ import annotations

import asyncio

import httpx

from tpwalk.scrape._reddit import RedditSource

# --- Fixture data ---

_REDDIT_PAGE1 = {
    "kind": "Listing",
    "data": {
        "children": [
            {
                "kind": "t3",
                "data": {
                    "id": "abc123",
                    "subreddit": "openwrt",
                    "selftext": "Found GPL at https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz",
                    "url": "https://static.tp-link.com/upload/gpl-code/2023/202301/20230115/GPL_AX3000.tar.gz",
                    "title": "TP-Link GPL source",
                    "permalink": "/r/openwrt/comments/abc123/tp_link_gpl/",
                },
            }
        ],
        "after": "t3_def456",
        "before": None,
    },
}

_REDDIT_PAGE2_EMPTY = {
    "kind": "Listing",
    "data": {"children": [], "after": None, "before": None},
}

_REDDIT_PAGE_WITH_TITLE_URL = {
    "kind": "Listing",
    "data": {
        "children": [
            {
                "kind": "t3",
                "data": {
                    "id": "xyz789",
                    "subreddit": "tplink",
                    "selftext": "No direct link here",
                    "url": "https://www.reddit.com/r/tplink/comments/xyz789/",
                    "title": "GPL from https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz is here",
                    "permalink": "/r/tplink/comments/xyz789/",
                },
            }
        ],
        "after": None,
        "before": None,
    },
}

_REDDIT_EMPTY = {
    "kind": "Listing",
    "data": {"children": [], "after": None, "before": None},
}


# --- Transport factories ---


def _make_extraction_transport() -> tuple[httpx.MockTransport, list[str]]:
    """Transport that serves: page with selftext URL + url URL on site-wide, then empty pages for subreddits."""
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)
        # site-wide search (first call, no after param) → page with URLs + after cursor
        if "reddit.com/search.json" in url and "after" not in url:
            return httpx.Response(200, json=_REDDIT_PAGE1)
        # paginated call with after → empty + no more pages
        if "reddit.com/search.json" in url and "after=t3_def456" in url:
            return httpx.Response(200, json=_REDDIT_PAGE2_EMPTY)
        # per-subreddit searches → empty
        if "/r/" in url and "search.json" in url:
            return httpx.Response(200, json=_REDDIT_EMPTY)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), request_log


def _make_pagination_transport() -> tuple[httpx.MockTransport, list[str]]:
    """Transport that serves a two-page paginated result for site-wide, empty for subreddits.

    Page 1: one child with a GPL URL, after='t3_def456'.
    Page 2 (requested with after=t3_def456): empty children, after=null.
    """
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)
        if "reddit.com/search.json" in url and "after" not in url:
            return httpx.Response(200, json=_REDDIT_PAGE1)
        if "after=t3_def456" in url:
            return httpx.Response(200, json=_REDDIT_PAGE2_EMPTY)
        if "/r/" in url and "search.json" in url:
            return httpx.Response(200, json=_REDDIT_EMPTY)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), request_log


def _make_subreddits_transport() -> tuple[httpx.MockTransport, list[str]]:
    """Transport that records all requests; all responses empty to keep result set clean."""
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.append(str(request.url))
        return httpx.Response(200, json=_REDDIT_EMPTY)

    return httpx.MockTransport(handler), request_log


def _make_title_transport() -> tuple[httpx.MockTransport, list[str]]:
    """Transport that returns a post with a GPL URL only in the title field."""
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.append(str(request.url))
        if "/r/" in str(request.url) and "search.json" in str(request.url):
            return httpx.Response(200, json=_REDDIT_PAGE_WITH_TITLE_URL)
        return httpx.Response(200, json=_REDDIT_EMPTY)

    return httpx.MockTransport(handler), request_log


def _make_ua_transport() -> tuple[httpx.MockTransport, list[dict[str, str]]]:
    """Transport that captures request headers to verify User-Agent presence."""
    header_log: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        header_log.append(dict(request.headers))
        return httpx.Response(200, json=_REDDIT_EMPTY)

    return httpx.MockTransport(handler), header_log


def _make_429_transport() -> httpx.MockTransport:
    """Transport that always returns 429 (simulating Reddit rate limiting)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "Too Many Requests"})

    return httpx.MockTransport(handler)


def _make_cursor_cycle_transport() -> tuple[httpx.MockTransport, list[int]]:
    """Transport that always echoes back the same non-null 'after' cursor.

    Simulates a Reddit API bug or cached/replayed response: every page returns
    after='t3_cycle_forever' with a single child containing a GPL URL. Without the
    page-cap + seen-cursor guard (CR-01 fix), _search would loop indefinitely — the
    no-op asyncio.sleep fixture ensures the loop is tight, so only the cap/cycle guard
    stops it.
    """
    call_count: list[int] = [0]
    gpl_url = "https://static.tp-link.com/resources/gpl/GPL_CycleTest.tar.gz"
    cycle_page = {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {"id": "cycle1", "subreddit": "openwrt", "selftext": f"source: {gpl_url}", "url": "", "title": ""},
                }
            ],
            "after": "t3_cycle_forever",  # same cursor on every page → cycle
            "before": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json=cycle_page)

    return httpx.MockTransport(handler), call_count


# --- Tests ---


def test_reddit_source_name() -> None:
    """RedditSource.name returns 'reddit' (D-10, D-18 stem convention)."""
    assert RedditSource().name == "reddit"


def test_reddit_search_extracts_selftext_and_url_fields() -> None:
    """URLs in selftext AND url fields are both extracted (SCRP-15).

    The mock page 1 has a GPL URL in selftext and a different one in url.
    Both must appear in the result set.
    """
    transport, _ = _make_extraction_transport()
    source = RedditSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert "https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz" in result
    assert "https://static.tp-link.com/upload/gpl-code/2023/202301/20230115/GPL_AX3000.tar.gz" in result


def test_reddit_search_extracts_title_field() -> None:
    """URLs embedded in post title fields are extracted (SCRP-15).

    The mock returns a post whose title contains a static.mercusys.com URL.
    That URL must appear in the result set.
    """
    transport, _ = _make_title_transport()
    source = RedditSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert "https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz" in result


def test_reddit_after_cursor_pagination() -> None:
    """Source paginates using the after cursor and stops when after is null (SCRP-15, D-10).

    Verifies:
    1. The second request carries after=t3_def456 in its query parameters.
    2. The source stops after page 2 (empty children + after=null).
    3. The request_log has exactly 2 calls to the site-wide search.json endpoint.
    """
    transport, request_log = _make_pagination_transport()
    source = RedditSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    site_wide_calls = [u for u in request_log if "reddit.com/search.json" in u]
    # First call: no after param; second call: after=t3_def456
    assert len(site_wide_calls) == 2, f"Expected 2 site-wide calls, got: {site_wide_calls}"
    assert any("after=t3_def456" in u for u in site_wide_calls), "Second call must carry after=t3_def456"


def test_reddit_queries_all_four_subreddits_and_site_wide() -> None:
    """All four named subreddits + site-wide endpoint are queried (D-12, SCRP-15).

    Checks that request_log includes:
    - /search.json (site-wide)
    - /r/openwrt/search.json with restrict_sr=1
    - /r/tplink/search.json with restrict_sr=1
    - /r/homelab/search.json with restrict_sr=1
    - /r/netsec/search.json with restrict_sr=1
    """
    transport, request_log = _make_subreddits_transport()
    source = RedditSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    site_wide = [u for u in request_log if "/search.json" in u and "/r/" not in u]
    openwrt = [u for u in request_log if "/r/openwrt/search.json" in u and "restrict_sr" in u]
    tplink = [u for u in request_log if "/r/tplink/search.json" in u and "restrict_sr" in u]
    homelab = [u for u in request_log if "/r/homelab/search.json" in u and "restrict_sr" in u]
    netsec = [u for u in request_log if "/r/netsec/search.json" in u and "restrict_sr" in u]

    assert site_wide, "Site-wide /search.json must be queried"
    assert openwrt, "r/openwrt must be queried with restrict_sr"
    assert tplink, "r/tplink must be queried with restrict_sr"
    assert homelab, "r/homelab must be queried with restrict_sr"
    assert netsec, "r/netsec must be queried with restrict_sr"


def test_reddit_user_agent_on_every_request() -> None:
    """Every request sends a descriptive User-Agent header (Pitfall 4, SCRP-15).

    A missing User-Agent causes immediate 429 on real Reddit.
    This test verifies the header is present and non-empty on all requests.
    """
    transport, header_log = _make_ua_transport()
    source = RedditSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    assert header_log, "No requests were made — at least one is expected"
    for headers in header_log:
        ua = headers.get("user-agent", "")
        assert ua, f"User-Agent header missing on a request: {headers}"
        assert ua != "python-httpx/0.28.1", "Must send a descriptive User-Agent, not the default httpx one"


def test_reddit_rate_limit_skip_gracefully() -> None:
    """A persistent 429 from Reddit does not raise; source returns empty set (SCRP-05, D-10).

    The mock always returns 429. After retry exhaustion the source skips and
    returns whatever URLs it managed to collect (empty in this case).
    """
    transport = _make_429_transport()
    source = RedditSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    # Must not raise despite persistent 429
    result = asyncio.run(_run())
    assert isinstance(result, set)
    # Persistent 429 means no URLs were found
    assert result == set()


def test_reddit_strips_trailing_punctuation_from_extracted_urls() -> None:
    """WR-01 regression: trailing sentence punctuation is stripped from extracted URLs.

    A post with selftext ending in a period ("...file.tar.gz.") must yield the
    clean URL without the trailing dot in the result set.
    """
    url_clean = "https://static.tp-link.com/resources/gpl/Trailing_Test.tar.gz"
    trailing_dot_page = {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "trail1",
                        "subreddit": "openwrt",
                        "selftext": f"See the source at {url_clean}.",
                        "url": "",
                        "title": "",
                    },
                }
            ],
            "after": None,
            "before": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search.json" in url:
            return httpx.Response(200, json=trailing_dot_page)
        return httpx.Response(404, json={})

    source = RedditSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert url_clean in result, f"Clean URL not found: {result}"
    assert f"{url_clean}." not in result, f"Trailing dot URL must not appear: {result}"


def test_reddit_cursor_cycle_terminates() -> None:
    """Regression: CR-01 — a repeating after cursor must not cause an infinite loop.

    The mock always returns after='t3_cycle_forever' with a non-empty children list,
    simulating a Reddit API bug or a cached/replayed response that would cause the
    old while-True loop to spin forever. asyncio.sleep is no-op'd by the autouse
    conftest fixture, so only the page cap + seen-cursor guard (_MAX_REDDIT_PAGES
    and the seen_cursors set) stops the loop. Without the CR-01 fix, this test
    hangs; with the fix it must complete and return the expected URL.
    """
    from tpwalk.scrape._reddit import _MAX_REDDIT_PAGES

    transport, call_count = _make_cursor_cycle_transport()
    source = RedditSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Must terminate and return the URL collected on page 1
    assert "https://static.tp-link.com/resources/gpl/GPL_CycleTest.tar.gz" in result

    # The loop must stop after at most _MAX_REDDIT_PAGES per search endpoint.
    # run() calls _search for 1 site-wide + 4 subreddit endpoints = 5 endpoints.
    # Each endpoint may make at most _MAX_REDDIT_PAGES requests before breaking.
    max_allowed = _MAX_REDDIT_PAGES * 5
    assert call_count[0] <= max_allowed, f"Pagination did not stop: {call_count[0]} requests made (cap is {max_allowed})"
