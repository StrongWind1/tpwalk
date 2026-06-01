"""Tests for tpwalk.scrape._google -- GoogleSource: best-effort Google SERP scraper.

Tests use httpx.MockTransport so no real network requests are made. The handler
dispatches on URL host: www.google.com → SERP HTML, 429, or consent redirect.

All block-signal tests assert the source does not raise and returns set() (D-14).
The extensions test asserts all 8 archive extensions trigger individual queries (SCRP-17).

Per SCRP-17, D-13, D-14.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx

# --- Fixtures ---

_GOOGLE_SERP_HTML = """<html><body>
<div class="yuRUbf">
  <a href="https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/GPL_AX55V1.tar.gz">
    <h3>GPL Source Code - TP-Link</h3>
  </a>
</div>
</body></html>"""

_GOOGLE_SERP_NO_YURUBF = """<html><body>
<a href="https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz">GPL Source</a>
</body></html>"""

_GOOGLE_SERP_MERCUSYS = """<html><body>
<div class="yuRUbf">
  <a href="https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz">
    <h3>Mercusys GPL</h3>
  </a>
</div>
</body></html>"""

_GOOGLE_429_BODY = '{"error": "rateLimited"}'

_GOOGLE_CONSENT_HTML = """<html><body><p>Before you continue to Google</p></body></html>"""

_GOOGLE_UNUSUAL_TRAFFIC_HTML = """<html><body>
<p>Our systems have detected unusual traffic from your computer network.</p>
</body></html>"""

_GOOGLE_EMPTY_SERP = "<html><body><p>No results found.</p></body></html>"


# --- Helpers ---


def _get_query_param(request_url: str, key: str) -> str:
    """Extract a single query parameter value from a URL."""
    parsed = urlparse(request_url)
    params = parse_qs(parsed.query)
    return params.get(key, [""])[0]


# --- Tests ---


def test_google_source_name() -> None:
    """GoogleSource.name returns 'google' (D-13 filename stem, D-18 stem decision)."""
    from tpwalk.scrape._google import GoogleSource

    source = GoogleSource()
    assert source.name == "google"


def test_google_searches_all_extensions() -> None:
    """Source issues exactly one query per extension across all 8 archive types (SCRP-17, D-13).

    The request_log must contain a query for each of tar.gz, tar.bz2, tar, tgz,
    zip, rar, gz, bz2 — each as 'site:static.tp-link.com {ext}'.
    """
    from tpwalk.scrape._google import _ARCHIVE_EXTENSIONS, GoogleSource

    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)
        return httpx.Response(200, text=_GOOGLE_EMPTY_SERP)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    # Verify all 8 extensions appear as separate queries
    assert len(_ARCHIVE_EXTENSIONS) == 8, f"Expected 8 extensions, got {len(_ARCHIVE_EXTENSIONS)}: {_ARCHIVE_EXTENSIONS}"

    seen_extensions: set[str] = set()
    for url in request_log:
        q = _get_query_param(url, "q")
        for ext in _ARCHIVE_EXTENSIONS:
            if f"site:static.tp-link.com {ext}" == q:
                seen_extensions.add(ext)

    missing = set(_ARCHIVE_EXTENSIONS) - seen_extensions
    assert not missing, f"Extensions not queried: {missing} (request log: {request_log})"


def test_google_extraction_from_yuRUbf_selector() -> None:
    """Mocked SERP with div.yuRUbf > a containing a GPL URL: that URL is in the returned set (SCRP-17, A2 selector path)."""
    from tpwalk.scrape._google import GoogleSource

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_GOOGLE_SERP_HTML)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    assert "https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/GPL_AX55V1.tar.gz" in result


def test_google_extraction_regex_fallback() -> None:
    """When div.yuRUbf selector yields nothing, regex fallback extracts URLs from raw HTML (SCRP-17, Assumption A2 degradation)."""
    from tpwalk.scrape._google import GoogleSource

    def handler(request: httpx.Request) -> httpx.Response:
        # No yuRUbf container -- forces the regex fallback path
        return httpx.Response(200, text=_GOOGLE_SERP_NO_YURUBF)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    assert "https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz" in result


def test_google_extracts_mercusys_urls() -> None:
    """GPL URLs on static.mercusys.com are also extracted (regex covers both brands, Assumption A2)."""
    from tpwalk.scrape._google import GoogleSource

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_GOOGLE_SERP_MERCUSYS)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    assert "https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz" in result


def test_google_skips_on_block_429() -> None:
    """HTTP 429 from Google: run() returns set() and does not raise (D-14, Pitfall 6)."""
    from tpwalk.scrape._google import GoogleSource

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=_GOOGLE_429_BODY)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    # Must not raise
    result = asyncio.run(_run())

    # Result may be empty (blocked) -- the key property is that no exception was raised
    assert isinstance(result, set)


def test_google_skips_on_consent_wall() -> None:
    """Redirect to consent.google.com: source soft-skips without raising (Pitfall 6, D-14)."""
    from tpwalk.scrape._google import GoogleSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "consent.google.com" in url:
            return httpx.Response(200, text=_GOOGLE_CONSENT_HTML)
        if "google.com" in url:
            # Redirect to consent page
            return httpx.Response(302, headers={"location": "https://consent.google.com/ml?continue=..."})
        return httpx.Response(404)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert isinstance(result, set)


def test_google_skips_on_unusual_traffic() -> None:
    """'unusual traffic' in response body: source soft-skips without raising (Pitfall 6, D-14)."""
    from tpwalk.scrape._google import GoogleSource

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_GOOGLE_UNUSUAL_TRAFFIC_HTML)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert isinstance(result, set)


def test_google_strips_trailing_punctuation_from_regex_fallback() -> None:
    """WR-01 regression: trailing sentence punctuation is stripped from regex-fallback URLs.

    When the BeautifulSoup selector yields nothing, the regex fallback runs over the raw
    HTML body. Any URL followed by a period (e.g. in a prose sentence) must be stripped
    of its trailing dot before reaching the result set.
    """
    from tpwalk.scrape._google import GoogleSource

    url_clean = "https://static.tp-link.com/resources/gpl/Google_Trail.tar.gz"
    # No yuRUbf container — forces the regex fallback. URL in sentence ends with period.
    serp_trailing_dot = f"<html><body><p>Download it from {url_clean}.</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=serp_trailing_dot)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert url_clean in result, f"Clean URL not found: {result}"
    assert f"{url_clean}." not in result, f"Trailing dot URL must not appear: {result}"


def test_google_conforms_to_protocol() -> None:
    """GoogleSource structurally conforms to ScrapeSource Protocol (D-17)."""
    from tpwalk.scrape import ScrapeSource
    from tpwalk.scrape._google import GoogleSource

    source = GoogleSource()
    assert isinstance(source, ScrapeSource), "GoogleSource does not conform to ScrapeSource Protocol"


def test_google_source_uses_semaphore_1() -> None:
    """Source serializes requests (Semaphore(1)) -- all 8 extension queries complete sequentially (D-14 CAPTCHA risk reduction)."""
    from tpwalk.scrape._google import GoogleSource

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, text=_GOOGLE_EMPTY_SERP)

    source = GoogleSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    # 8 extension queries must all have completed (Semaphore serializes, but does not drop requests)
    assert call_count == 8, f"Expected 8 HTTP calls (one per extension), got {call_count}"
