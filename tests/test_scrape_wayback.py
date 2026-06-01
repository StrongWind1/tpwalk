"""Tests for tpwalk.scrape._wayback -- Wayback CDX resumeKey pagination, prefix queries, retry, dedup.

Tests use inline JSON fixtures and httpx.MockTransport so no real network requests are made.
Per SCRP-06, SCRP-07, D-07, D-08.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx

# --- Canned CDX response fixtures ---
# CDX API with output=json returns one JSON array per line (line-delimited, NOT a single array).
# Each line is independently parseable via json.loads(line).

# Single page, no resume key: header + 2 data rows
_SINGLE_PAGE_RESPONSE = '["original"]\n["https://static.tp-link.com/resources/gpl/11N_GPL.tgz"]\n["https://static.tp-link.com/resources/gpl/1201-gpl.tar.gz"]\n'

# Page 1 with resumeKey: header + 2 data rows + delimiter + resume key
_PAGE1_WITH_RESUME = '["original"]\n["https://static.tp-link.com/resources/gpl/page1_a.tar.gz"]\n["https://static.tp-link.com/resources/gpl/page1_b.tar.gz"]\n[]\n["eJxLzs_VKSnQzcnMy9Yp"]\n'

# Page 2 without resumeKey: header + 2 new data rows (no delimiter, no resume key)
_PAGE2_NO_RESUME = '["original"]\n["https://static.tp-link.com/resources/gpl/page2_a.tar.gz"]\n["https://static.tp-link.com/resources/gpl/page2_b.tar.gz"]\n'

# Page 1 ending with url_X, page 2 starting with url_X (boundary dedup test)
_BOUNDARY_PAGE1 = '["original"]\n["https://static.tp-link.com/resources/gpl/unique_a.tar.gz"]\n["https://static.tp-link.com/resources/gpl/boundary.tar.gz"]\n[]\n["eJxLzs_boundary_key"]\n'
_BOUNDARY_PAGE2 = '["original"]\n["https://static.tp-link.com/resources/gpl/boundary.tar.gz"]\n["https://static.tp-link.com/resources/gpl/unique_b.tar.gz"]\n'

# Mixed URL types for no-filtering test (D-08): archive, HTML, CSS, image
_MIXED_URLS_RESPONSE = '["original"]\n["https://static.tp-link.com/resources/gpl/archive.tar.gz"]\n["https://static.tp-link.com/en/some-page.html"]\n["https://static.tp-link.com/css/style.css"]\n["https://static.tp-link.com/images/product.jpg"]\n'

# Empty response: header only, no data rows
_EMPTY_RESPONSE = '["original"]\n'


# --- Helpers ---


def _extract_url_param(request_url: str) -> str:
    """Extract the 'url' query parameter from a CDX request URL."""
    parsed = urlparse(request_url)
    params = parse_qs(parsed.query)
    return params.get("url", [""])[0]


def _make_wayback_transport(
    *,
    prefix_responses: dict[str, list[str]] | None = None,
    error_prefixes: dict[str, list[int]] | None = None,
) -> tuple[httpx.MockTransport, list[str]]:
    """Build a MockTransport for Wayback CDX tests.

    Args:
        prefix_responses: Map of url prefix -> list of response bodies (one per page).
        error_prefixes: Map of url prefix -> list of HTTP status codes to return.

    Returns:
        (transport, request_log) where request_log tracks all request URLs.
    """
    prefix_responses = prefix_responses or {}
    error_prefixes = error_prefixes or {}
    request_log: list[str] = []
    # Track page index per prefix for multi-page responses
    page_counters: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)

        if "web.archive.org/cdx/search/cdx" not in url:
            return httpx.Response(404)

        url_param = _extract_url_param(url)

        # Check error prefixes first
        if url_param in error_prefixes:
            statuses = error_prefixes[url_param]
            idx = page_counters.get(url_param, 0)
            page_counters[url_param] = idx + 1
            if idx < len(statuses):
                return httpx.Response(statuses[idx])
            return httpx.Response(200, text=_EMPTY_RESPONSE)

        # Serve canned responses
        if url_param in prefix_responses:
            pages = prefix_responses[url_param]
            idx = page_counters.get(url_param, 0)
            page_counters[url_param] = idx + 1
            if idx < len(pages):
                return httpx.Response(200, text=pages[idx])
            return httpx.Response(200, text=_EMPTY_RESPONSE)

        # Default: empty response for unrecognized prefixes
        return httpx.Response(200, text=_EMPTY_RESPONSE)

    transport = httpx.MockTransport(handler)
    return transport, request_log


# --- Tests ---


def test_wayback_source_name() -> None:
    """WaybackSource.name is 'wayback_cdx' (D-14)."""
    from tpwalk.scrape._wayback import WaybackSource

    source = WaybackSource()
    assert source.name == "wayback_cdx"


def test_wayback_queries_all_prefixes() -> None:
    """WaybackSource.run() issues CDX queries for all 4 URL prefixes (D-01, D-02, D-04; broad superset dropped)."""
    from tpwalk.scrape._wayback import WaybackSource

    expected_prefixes = {
        "static.tp-link.com/resources/gpl/*",
        "static.tp-link.com/upload/gpl-code/*",
        "static.tp-link.com/20*",
        "static.mercusys.com/gpl/*",
    }

    # All prefixes return the same simple response
    prefix_responses = {p: [_SINGLE_PAGE_RESPONSE] for p in expected_prefixes}
    transport, request_log = _make_wayback_transport(prefix_responses=prefix_responses)

    source = WaybackSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    # Extract url= params from all requests
    queried_prefixes = set()
    for req_url in request_log:
        if "web.archive.org/cdx/search/cdx" in req_url:
            queried_prefixes.add(_extract_url_param(req_url))

    assert expected_prefixes.issubset(queried_prefixes), f"Missing prefixes: {expected_prefixes - queried_prefixes}"


def test_wayback_resume_key_pagination() -> None:
    """Wayback CDX resumeKey pagination fetches multiple pages and unions results (SCRP-07)."""
    from tpwalk.scrape._wayback import WaybackSource

    # Use the date-hierarchical prefix as the paginated subject (one of the 4 queried prefixes)
    prefix = "static.tp-link.com/upload/gpl-code/*"
    other_prefixes = {
        "static.tp-link.com/resources/gpl/*",
        "static.tp-link.com/20*",
        "static.mercusys.com/gpl/*",
    }
    prefix_responses = {prefix: [_PAGE1_WITH_RESUME, _PAGE2_NO_RESUME]}
    # Other prefixes return empty
    for p in other_prefixes:
        prefix_responses[p] = [_EMPTY_RESPONSE]

    transport, _log = _make_wayback_transport(prefix_responses=prefix_responses)

    source = WaybackSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Verify multiple pages were fetched (at least 2 requests to the paginated prefix)
    # Note: URL params are percent-encoded in the raw log, so match via _extract_url_param
    paginated_requests = [u for u in _log if "web.archive.org" in u and _extract_url_param(u) == prefix]
    assert len(paginated_requests) >= 2, f"Expected >= 2 requests for paginated prefix, got {len(paginated_requests)}: {_log}"

    # Should have URLs from both pages
    assert "https://static.tp-link.com/resources/gpl/page1_a.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/page1_b.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/page2_a.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/page2_b.tar.gz" in result


def test_wayback_boundary_dedup() -> None:
    """Boundary record appearing on both page 1 and page 2 is deduplicated (set behavior)."""
    from tpwalk.scrape._wayback import WaybackSource

    prefix = "static.tp-link.com/upload/gpl-code/*"
    other_prefixes = {
        "static.tp-link.com/resources/gpl/*",
        "static.tp-link.com/20*",
        "static.mercusys.com/gpl/*",
    }
    prefix_responses = {prefix: [_BOUNDARY_PAGE1, _BOUNDARY_PAGE2]}
    for p in other_prefixes:
        prefix_responses[p] = [_EMPTY_RESPONSE]

    transport, _ = _make_wayback_transport(prefix_responses=prefix_responses)

    source = WaybackSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # boundary.tar.gz appears on both pages but should be in set exactly once
    boundary_url = "https://static.tp-link.com/resources/gpl/boundary.tar.gz"
    assert boundary_url in result
    # Total unique URLs: unique_a, boundary, unique_b = 3
    expected = {
        "https://static.tp-link.com/resources/gpl/unique_a.tar.gz",
        "https://static.tp-link.com/resources/gpl/boundary.tar.gz",
        "https://static.tp-link.com/resources/gpl/unique_b.tar.gz",
    }
    assert expected.issubset(result)


def test_no_url_filtering() -> None:
    """All URLs returned by CDX are kept, including HTML, CSS, images (D-08)."""
    from tpwalk.scrape._wayback import WaybackSource

    prefix = "static.tp-link.com/upload/gpl-code/*"
    other_prefixes = {
        "static.tp-link.com/resources/gpl/*",
        "static.tp-link.com/20*",
        "static.mercusys.com/gpl/*",
    }
    prefix_responses = {prefix: [_MIXED_URLS_RESPONSE]}
    for p in other_prefixes:
        prefix_responses[p] = [_EMPTY_RESPONSE]

    transport, _ = _make_wayback_transport(prefix_responses=prefix_responses)

    source = WaybackSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # ALL URL types must be present -- no filtering per D-08
    assert "https://static.tp-link.com/resources/gpl/archive.tar.gz" in result
    assert "https://static.tp-link.com/en/some-page.html" in result
    assert "https://static.tp-link.com/css/style.css" in result
    assert "https://static.tp-link.com/images/product.jpg" in result


def test_retry_exponential_backoff(monkeypatch: object) -> None:
    """Retry 3 times with exponential backoff on 503, then succeed on 4th attempt (D-07).

    Verifies 4 total requests for the failing prefix (3 retries + 1 success).
    Monkeypatches asyncio.sleep to avoid real delays in tests.
    """
    from unittest.mock import AsyncMock

    from tpwalk.scrape import _wayback as wayback_mod
    from tpwalk.scrape._wayback import WaybackSource

    # Monkeypatch asyncio.sleep to a no-op so tests run fast
    mock_sleep = AsyncMock()
    monkeypatch.setattr(wayback_mod.asyncio, "sleep", mock_sleep)  # type: ignore[attr-defined]

    # One prefix returns 503 three times then 200 on the 4th
    failing_prefix = "static.tp-link.com/resources/gpl/*"
    other_prefixes = {
        "static.tp-link.com/upload/gpl-code/*",
        "static.tp-link.com/20*",
        "static.mercusys.com/gpl/*",
    }

    # Custom handler: 3 errors then success for failing_prefix, empty for others.
    request_log: list[str] = []
    call_counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)

        if "web.archive.org/cdx/search/cdx" not in url:
            return httpx.Response(404)

        url_param = _extract_url_param(url)
        count = call_counts.get(url_param, 0)
        call_counts[url_param] = count + 1

        if url_param == failing_prefix:
            if count < 3:
                return httpx.Response(503)
            # 4th request: success
            return httpx.Response(200, text=_SINGLE_PAGE_RESPONSE)

        if url_param in other_prefixes:
            return httpx.Response(200, text=_EMPTY_RESPONSE)

        return httpx.Response(200, text=_EMPTY_RESPONSE)

    transport = httpx.MockTransport(handler)

    source = WaybackSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # URLs from the eventually-successful prefix should be present
    assert "https://static.tp-link.com/resources/gpl/11N_GPL.tgz" in result
    assert "https://static.tp-link.com/resources/gpl/1201-gpl.tar.gz" in result

    # Should have made 4 requests to the failing prefix (3 retries + 1 success)
    failing_requests = [u for u in request_log if "web.archive.org" in u and _extract_url_param(u) == failing_prefix]
    assert len(failing_requests) == 4, f"Expected 4 requests to failing prefix, got {len(failing_requests)}"

    # Verify sleep was called with exponential backoff values
    sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
    assert 2.0 in sleep_args, f"Expected 2.0s backoff, got: {sleep_args}"
    assert 4.0 in sleep_args, f"Expected 4.0s backoff, got: {sleep_args}"
    assert 8.0 in sleep_args, f"Expected 8.0s backoff, got: {sleep_args}"


def test_wayback_skip_after_max_retries(monkeypatch: object) -> None:
    """After max retries exhausted, prefix is skipped. Other prefixes still produce URLs."""
    from unittest.mock import AsyncMock

    from tpwalk.scrape import _wayback as wayback_mod
    from tpwalk.scrape._wayback import WaybackSource

    # Monkeypatch asyncio.sleep to a no-op so tests run fast
    monkeypatch.setattr(wayback_mod.asyncio, "sleep", AsyncMock())  # type: ignore[attr-defined]

    always_failing = "static.tp-link.com/resources/gpl/*"
    good_prefix = "static.tp-link.com/upload/gpl-code/*"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "web.archive.org/cdx/search/cdx" not in url:
            return httpx.Response(404)

        url_param = _extract_url_param(url)

        if url_param == always_failing:
            return httpx.Response(503)

        if url_param == good_prefix:
            return httpx.Response(200, text=_SINGLE_PAGE_RESPONSE)

        return httpx.Response(200, text=_EMPTY_RESPONSE)

    transport = httpx.MockTransport(handler)

    source = WaybackSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Good prefix URLs should be present
    assert "https://static.tp-link.com/resources/gpl/11N_GPL.tgz" in result
    # No exception raised (the whole point of this test)


def test_wayback_empty_response() -> None:
    """CDX response with only header row and no data returns empty set (no crash)."""
    from tpwalk.scrape._wayback import WaybackSource

    all_prefixes = {
        "static.tp-link.com/resources/gpl/*",
        "static.tp-link.com/upload/gpl-code/*",
        "static.tp-link.com/20*",
        "static.mercusys.com/gpl/*",
    }
    prefix_responses = {p: [_EMPTY_RESPONSE] for p in all_prefixes}
    transport, _ = _make_wayback_transport(prefix_responses=prefix_responses)

    source = WaybackSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert result == set()
