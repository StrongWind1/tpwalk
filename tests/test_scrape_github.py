"""Tests for tpwalk/scrape/_github.py -- GitHubSearchSource and TPLinkGitHubSource.

All tests use httpx.MockTransport; no real network calls are ever made.
See 04-RESEARCH.md "Mock Response Shapes for Tests" and 04-PATTERNS.md
"tests/test_scrape_github.py" for fixture shapes and handler patterns.

Per SCRP-08, SCRP-09, SCRP-10, SCRP-16, D-01, D-02.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared fixture data (from RESEARCH.md §Mock Response Shapes for Tests)
# ---------------------------------------------------------------------------

_GPL_URL_CODE = "https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/GPL_AX55V1.tar.gz"
_GPL_URL_ISSUE = "https://static.tp-link.com/resources/gpl/GPL_ArcherC7v5.tar.gz"
_GPL_URL_MERCUSYS = "https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz"

_GITHUB_CODE_RESPONSE: dict[str, Any] = {
    "total_count": 12,
    "incomplete_results": False,
    "items": [
        {
            "name": "README.md",
            "html_url": "https://github.com/user/repo/blob/main/README.md",
            "url": "https://api.github.com/repositories/12345/contents/README.md",
            "text_matches": [
                {
                    "fragment": f"Download GPL from {_GPL_URL_CODE} here",
                    "matches": [{"text": _GPL_URL_CODE, "indices": [18, 90]}],
                }
            ],
        }
    ],
}

_GITHUB_ISSUE_RESPONSE: dict[str, Any] = {
    "total_count": 5,
    "incomplete_results": False,
    "items": [
        {
            "number": 42,
            "html_url": "https://github.com/user/repo/issues/42",
            "text_matches": [
                {
                    "fragment": f"Please use {_GPL_URL_ISSUE} for source",
                    "matches": [{"text": _GPL_URL_ISSUE, "indices": [11, 80]}],
                }
            ],
        }
    ],
}

_GITHUB_MERCUSYS_RESPONSE: dict[str, Any] = {
    "total_count": 3,
    "incomplete_results": False,
    "items": [
        {
            "name": "build.sh",
            "html_url": "https://github.com/user/repo2/blob/main/build.sh",
            "text_matches": [
                {
                    "fragment": f"wget {_GPL_URL_MERCUSYS}",
                    "matches": [{"text": _GPL_URL_MERCUSYS, "indices": [5, 60]}],
                }
            ],
        }
    ],
}

_GITHUB_EMPTY_RESPONSE: dict[str, Any] = {
    "total_count": 0,
    "incomplete_results": False,
    "items": [],
}

# Rate-limit 403 response with Retry-After header
_GITHUB_RATE_LIMIT_403_HEADERS: dict[str, str] = {
    "X-RateLimit-Remaining": "0",
    "X-RateLimit-Reset": "9999999999",
    "Retry-After": "60",
}

# Mock org repos list for TPLinkGitHubSource
_TPLINK_ORG_REPOS: list[dict[str, Any]] = [
    {
        "name": "Romesburg",
        "html_url": "https://github.com/TP-LINK/Romesburg",
        "has_wiki": False,
    }
]

_TPLINK_ORG_CODE_RESPONSE: dict[str, Any] = {
    "total_count": 1,
    "incomplete_results": False,
    "items": [
        {
            "name": "README.md",
            "html_url": "https://github.com/TP-LINK/Romesburg/blob/main/README.md",
            "text_matches": [
                {
                    "fragment": f"source at {_GPL_URL_CODE}",
                    "matches": [{"text": _GPL_URL_CODE, "indices": [10, 90]}],
                }
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Transport factory helpers
# ---------------------------------------------------------------------------


def _make_code_search_transport(
    code_response: dict[str, Any],
    issue_response: dict[str, Any] | None = None,
) -> tuple[httpx.MockTransport, list[str]]:
    """Return a MockTransport that serves code and issue search responses.

    The request_log captures every URL string that the handler sees, allowing
    tests to assert which queries were made (SCRP-09 test uses this to verify
    the Mercusys prefix is queried).
    """
    request_log: list[str] = []
    _issue_resp = issue_response or _GITHUB_EMPTY_RESPONSE

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)
        if "/search/code" in url:
            return httpx.Response(200, json=code_response)
        if "/search/issues" in url:
            return httpx.Response(200, json=_issue_resp)
        return httpx.Response(404, json={"message": "Not found"})

    return httpx.MockTransport(handler), request_log


def _make_rate_limit_then_ok_transport(
    ok_response: dict[str, Any],
) -> tuple[httpx.MockTransport, list[int]]:
    """First request returns 403+Retry-After, second returns ok_response.

    call_count tracks how many requests were made to verify the source retried.
    """
    call_count: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(403, json={"message": "secondary rate limit"}, headers=_GITHUB_RATE_LIMIT_403_HEADERS)
        # Subsequent calls: return empty (only need to survive the rate-limit, not collect URLs)
        return httpx.Response(200, json=ok_response)

    return httpx.MockTransport(handler), call_count


def _make_pagination_transport(pages: int) -> tuple[httpx.MockTransport, list[int]]:
    """Return pages of 100 items then empty page; records page numbers seen."""
    pages_seen: list[int] = []
    _item = {
        "name": "f.md",
        "html_url": "https://github.com/u/r/blob/main/f.md",
        "text_matches": [],
    }
    _full_page = {"total_count": 1200, "incomplete_results": False, "items": [_item] * 100}
    _empty = _GITHUB_EMPTY_RESPONSE

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        page_str = request.url.params.get("page", "1")
        page = int(page_str)
        pages_seen.append(page)
        if "/search/code" in url or "/search/issues" in url:
            if page <= pages:
                return httpx.Response(200, json=_full_page)
            return httpx.Response(200, json=_empty)
        return httpx.Response(404)

    return httpx.MockTransport(handler), pages_seen


def _make_tplink_org_transport() -> tuple[httpx.MockTransport, list[str]]:
    """Mock for TPLinkGitHubSource: org repos endpoint + code search with user:TP-LINK."""
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)
        if "/orgs/TP-LINK/repos" in url or "/users/TP-LINK/repos" in url:
            return httpx.Response(200, json=_TPLINK_ORG_REPOS)
        if "/search/code" in url:
            return httpx.Response(200, json=_TPLINK_ORG_CODE_RESPONSE)
        if "/search/issues" in url:
            return httpx.Response(200, json=_GITHUB_EMPTY_RESPONSE)
        return httpx.Response(404, json={"message": "Not found"})

    return httpx.MockTransport(handler), request_log


def _make_empty_org_transport() -> httpx.MockTransport:
    """All API calls return empty results -- TPLinkGitHubSource must return set()."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": [], "repos": []})

    return httpx.MockTransport(handler)


def _make_http_date_retry_after_transport(
    ok_response: dict[str, Any],
) -> tuple[httpx.MockTransport, list[int]]:
    """First request returns 403 with an HTTP-date Retry-After; subsequent calls return ok.

    The HTTP-date value "Wed, 21 Oct 2025 07:28:00 GMT" is a spec-legal form of
    Retry-After (RFC 9110 §10.2.3). The old code called float() on it and raised
    ValueError; the CR-02 fix must handle it without crashing.
    """
    call_count: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                403,
                json={"message": "secondary rate limit"},
                headers={"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"},
            )
        return httpx.Response(200, json=ok_response)

    return httpx.MockTransport(handler), call_count


# ---------------------------------------------------------------------------
# GitHubSearchSource tests (SCRP-08, SCRP-09, SCRP-10)
# ---------------------------------------------------------------------------


def test_github_code_search_extracts_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-08: code search response with text_match fragment yields the GPL URL in run()."""
    from tpwalk.scrape._github import GitHubSearchSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    transport, _ = _make_code_search_transport(code_response=_GITHUB_CODE_RESPONSE)
    source = GitHubSearchSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert _GPL_URL_CODE in result


def test_github_issue_search_extracts_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-08: issue search response with fragment containing GPL URL yields that URL."""
    from tpwalk.scrape._github import GitHubSearchSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    transport, _ = _make_code_search_transport(
        code_response=_GITHUB_EMPTY_RESPONSE,
        issue_response=_GITHUB_ISSUE_RESPONSE,
    )
    source = GitHubSearchSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert _GPL_URL_ISSUE in result


def test_github_searches_mercusys_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-09: source issues a query containing the Mercusys GPL prefix; URL in result."""
    from tpwalk.scrape._github import GitHubSearchSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")

    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)
        # Serve mercusys URL only when query contains mercusys
        q = request.url.params.get("q", "")
        if "mercusys" in q and "/search/code" in url:
            return httpx.Response(200, json=_GITHUB_MERCUSYS_RESPONSE)
        return httpx.Response(200, json=_GITHUB_EMPTY_RESPONSE)

    transport = httpx.MockTransport(handler)
    source = GitHubSearchSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    # Assert a request containing "mercusys" was made
    assert any("mercusys" in url for url in request_log), f"No mercusys query found in {request_log}"
    assert _GPL_URL_MERCUSYS in result


def test_github_missing_token_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-10/D-02: GITHUB_TOKEN unset -> run() returns set() and never hits the transport."""
    from tpwalk.scrape._github import GitHubSearchSource

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    source = GitHubSearchSource()

    def handler(request: httpx.Request) -> httpx.Response:
        # Transport should never be hit when token is absent (D-02)
        raise AssertionError("HTTP request made despite missing GITHUB_TOKEN")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert result == set()


def test_github_secondary_rate_limit_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-10/D-05: 403 with Retry-After causes asyncio.sleep(60.0) then retry."""
    from tpwalk.scrape._github import GitHubSearchSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    transport, call_count = _make_rate_limit_then_ok_transport(ok_response=_GITHUB_EMPTY_RESPONSE)
    source = GitHubSearchSource()

    async def _run() -> set[str]:
        with patch("tpwalk.scrape._github.asyncio.sleep", fake_sleep):
            async with httpx.AsyncClient(transport=transport) as client:
                return await source.run(client=client)

    asyncio.run(_run())
    # asyncio.sleep must have been called with 60.0 (the Retry-After header value)
    assert 60.0 in sleep_calls, f"Expected sleep(60.0) not found; calls: {sleep_calls}"
    # Source must have retried (made more than 1 request)
    assert call_count[0] > 1, "Expected retry after rate-limit 403"


def test_github_pagination_stops_at_1000(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-10/Pitfall 3: pagination stops at page 10 (1,000 results cap), no error raised."""
    from tpwalk.scrape import _github as gh_module
    from tpwalk.scrape._github import GitHubSearchSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    # Provide 15 pages of results so we can confirm the source stops at page 10
    transport, pages_seen = _make_pagination_transport(pages=15)
    source = GitHubSearchSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await source.run(client=client)

    asyncio.run(_run())
    # No page beyond _MAX_GITHUB_PAGES should appear in code or issue search
    max_page = gh_module._MAX_GITHUB_PAGES
    assert all(p <= max_page for p in pages_seen), f"Pages beyond {max_page} requested: {pages_seen}"


# ---------------------------------------------------------------------------
# TPLinkGitHubSource tests (SCRP-16)
# ---------------------------------------------------------------------------


def test_tplink_github_source_name() -> None:
    """SCRP-16: TPLinkGitHubSource.name returns 'tplink_github'."""
    from tpwalk.scrape._github import TPLinkGitHubSource

    assert TPLinkGitHubSource().name == "tplink_github"


def test_tplink_github_missing_token_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-16/D-02: GITHUB_TOKEN unset -> TPLinkGitHubSource returns set() (no network)."""
    from tpwalk.scrape._github import TPLinkGitHubSource

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    source = TPLinkGitHubSource()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP request made despite missing GITHUB_TOKEN")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert result == set()


def test_tplink_github_scans_org_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-16: TP-LINK org repos listed and GPL URL from code-search fragment returned."""
    from tpwalk.scrape._github import TPLinkGitHubSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    transport, _request_log = _make_tplink_org_transport()
    source = TPLinkGitHubSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert _GPL_URL_CODE in result


def test_tplink_github_returns_empty_when_org_is_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRP-16: org has no matching content -> run() returns empty set without raising."""
    from tpwalk.scrape._github import TPLinkGitHubSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    transport = _make_empty_org_transport()
    source = TPLinkGitHubSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await source.run(client=client)

    # Must not raise; must return a set (possibly empty)
    result = asyncio.run(_run())
    assert isinstance(result, set)


def test_tplink_github_org_repos_uses_standard_accept_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-04 regression: _fetch_org_repos uses application/vnd.github+json, not the search text-match Accept.

    The text-match media type is documented only for /search/* endpoints. Sending it
    to /orgs/*/repos is fragile against future API stricter media-type handling.
    This test captures the Accept header sent to the repos endpoint and asserts it
    is the standard REST media type, not the search-preview one.

    The token value is never captured or asserted (ASVS V7, T-04-01).
    """
    from tpwalk.scrape._github import TPLinkGitHubSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")

    # Capture per-endpoint Accept headers
    accept_by_path: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        accept_by_path[path] = request.headers.get("accept", "")
        if "/orgs/TP-LINK/repos" in path or "/users/TP-LINK/repos" in path:
            return httpx.Response(200, json=_TPLINK_ORG_REPOS)
        if "/search/code" in path:
            return httpx.Response(200, json=_GITHUB_EMPTY_RESPONSE)
        if "/search/issues" in path:
            return httpx.Response(200, json=_GITHUB_EMPTY_RESPONSE)
        return httpx.Response(404)

    source = TPLinkGitHubSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await source.run(client=client)

    asyncio.run(_run())

    # At least one repos endpoint must have been called
    repo_paths = [p for p in accept_by_path if "/repos" in p]
    assert repo_paths, f"No repos endpoint was called. Paths seen: {list(accept_by_path)}"

    for rp in repo_paths:
        accept = accept_by_path[rp]
        assert "vnd.github+json" in accept or accept == "application/vnd.github+json", f"Repos endpoint {rp!r} must use standard Accept header, got: {accept!r}"
        assert "text-match" not in accept, f"Repos endpoint {rp!r} must NOT use text-match Accept header, got: {accept!r}"

    # Search endpoints should still use the text-match header
    search_paths = [p for p in accept_by_path if "/search/" in p]
    for sp in search_paths:
        accept = accept_by_path[sp]
        assert "text-match" in accept, f"Search endpoint {sp!r} should use text-match Accept header, got: {accept!r}"


def test_github_fragment_strips_trailing_punctuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-01 regression: trailing punctuation in a text_match fragment is stripped.

    A fragment like "Download from https://static.tp-link.com/gpl/file.tar.gz."
    must yield the URL without the trailing period. Verifies the rstrip(_TRAILING_PUNCT)
    fix prevents phantom dot-terminated URLs from reaching the output set.
    """
    from tpwalk.scrape._github import GitHubSearchSource

    url_clean = "https://static.tp-link.com/resources/gpl/Trailing_Test.tar.gz"
    response_with_trailing_dot: dict[str, object] = {
        "total_count": 1,
        "incomplete_results": False,
        "items": [
            {
                "name": "readme.md",
                "html_url": "https://github.com/user/repo/blob/main/readme.md",
                "text_matches": [
                    {
                        # Trailing period after the URL — a typical prose sentence ending.
                        "fragment": f"Download from {url_clean}.",
                        "matches": [{"text": url_clean, "indices": [14, 80]}],
                    }
                ],
            }
        ],
    }

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/search/code" in str(request.url) or "/search/issues" in str(request.url):
            return httpx.Response(200, json=response_with_trailing_dot)
        return httpx.Response(404)

    source = GitHubSearchSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert url_clean in result, f"Clean URL not found: {result}"
    assert f"{url_clean}." not in result, f"Trailing dot URL must not appear: {result}"


def test_github_http_date_retry_after_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: CR-02 — HTTP-date Retry-After header must not raise ValueError.

    RFC 9110 §10.2.3 allows Retry-After to be "Wed, 21 Oct 2025 07:28:00 GMT"
    (HTTP-date form). The old code called float("Wed, 21 Oct 2025 07:28:00 GMT")
    which raises ValueError, aborting the entire GitHub source. The CR-02 fix adds
    _parse_retry_seconds() which handles both numeric and HTTP-date forms gracefully,
    returning 0.0 for a date in the past (asyncio.sleep is no-op'd in tests anyway).

    This test verifies:
    1. run() does not raise on an HTTP-date Retry-After.
    2. The source retries (makes more than 1 request).
    3. The source returns results collected from the successful follow-up queries.
    """
    from tpwalk.scrape._github import GitHubSearchSource

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")

    transport, call_count = _make_http_date_retry_after_transport(ok_response=_GITHUB_CODE_RESPONSE)
    source = GitHubSearchSource()

    async def _run() -> set[str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await source.run(client=client)

    # Must not raise ValueError on the HTTP-date Retry-After
    result = asyncio.run(_run())

    # Source must have retried (first call was the 403, subsequent calls were 200)
    assert call_count[0] > 1, "Expected at least one retry after the HTTP-date rate-limit 403"

    # GPL URL from the ok_response must appear in the result
    assert _GPL_URL_CODE in result, f"Expected GPL URL in result; got: {result}"
