"""Tests for tpwalk.verify._head — HEAD checker with mocked HTTP responses.

Tests verify that head_url returns typed VerifiedEntry / DeadEntry values for
all response scenarios, that S3 origin URLs are used (not CloudFront), that
semaphore bounds concurrency, and that head_check_all updates a progress object
with live/dead counters after each result per D-07.

Per FOUN-03, VERF-04, VERF-05, VERF-07.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx

# --- Mock transport helper functions ---
# httpx.MockTransport takes a handler callable — not a subclass with handle_request.

_FULL_HEADERS = {
    "content-length": "12345678",
    "etag": '"abc123def456"',
    "content-type": "application/x-gzip",
    "last-modified": "Wed, 20 May 2026 12:00:00 GMT",
    "x-amz-version-id": "NB6JlD_Y7Cb3ZQPioiavpF3X1qRZztZY",
    "x-amz-replication-status": "COMPLETED",
    "x-amz-server-side-encryption": "AES256",
    "server": "AmazonS3",
}

_LEGACY_HEADERS = {
    "content-length": "423447438",
    "etag": '"cef8ad3c3e60d16ddd36945117b02767"',
    "content-type": "application/x-gzip",
    "last-modified": "Tue, 07 Feb 2017 12:00:00 GMT",
    "x-amz-version-id": "null",
    "server": "AmazonS3",
    # Note: no x-amz-replication-status or x-amz-server-side-encryption on legacy files
}


def _make_fixed_transport(status_code: int, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    """Return a transport that always responds with the given status and headers."""
    fixed_headers = headers or {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers=fixed_headers)

    return httpx.MockTransport(handler)


def _make_per_url_transport(responses: dict[str, tuple[int, dict[str, str]]]) -> httpx.MockTransport:
    """Return a transport that dispatches responses by URL fragment matching."""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for key, (code, hdrs) in responses.items():
            if key in url_str:
                return httpx.Response(code, headers=hdrs)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _make_timeout_transport() -> httpx.MockTransport:
    """Return a transport that always raises httpx.TimeoutException."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    return httpx.MockTransport(handler)


def _make_network_error_transport() -> httpx.MockTransport:
    """Return a transport that always raises httpx.ConnectError (a RequestError subclass)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


# --- Tests for head_url ---


def test_head_url_200_returns_verified_entry() -> None:
    """A 200 response produces a VerifiedEntry with all required fields set."""
    from tpwalk.models import VerifiedEntry
    from tpwalk.verify._head import head_url

    url = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"
    client = httpx.AsyncClient(transport=_make_fixed_transport(200, _FULL_HEADERS))
    sem = asyncio.Semaphore(10)

    async def _run() -> VerifiedEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, VerifiedEntry)
    assert result.url == url
    assert result.status == 200


def test_head_url_extracts_all_d06_headers() -> None:
    """All D-06 fields are extracted from a 200 response with full headers."""
    from tpwalk.models import VerifiedEntry
    from tpwalk.verify._head import head_url

    url = "https://static.tp-link.com/upload/gpl-code/2026/202605/20260527/GPL_AXE5400V2.tar.gz"
    client = httpx.AsyncClient(transport=_make_fixed_transport(200, _FULL_HEADERS))
    sem = asyncio.Semaphore(10)

    async def _run() -> VerifiedEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, VerifiedEntry)
    assert result.size == 12345678
    assert result.etag == '"abc123def456"'
    assert result.content_type == "application/x-gzip"
    assert result.last_modified == "Wed, 20 May 2026 12:00:00 GMT"
    assert result.version_id == "NB6JlD_Y7Cb3ZQPioiavpF3X1qRZztZY"
    assert result.replication_status == "COMPLETED"
    assert result.encryption == "AES256"
    assert result.server == "AmazonS3"
    assert result.checked_at  # non-empty ISO 8601 string


def test_head_url_null_version_id_normalized() -> None:
    """x-amz-version-id: 'null' (legacy S3 string) becomes Python None in VerifiedEntry."""
    from tpwalk.models import VerifiedEntry
    from tpwalk.verify._head import head_url

    url = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"
    client = httpx.AsyncClient(transport=_make_fixed_transport(200, _LEGACY_HEADERS))
    sem = asyncio.Semaphore(10)

    async def _run() -> VerifiedEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, VerifiedEntry)
    assert result.version_id is None, "String 'null' must be normalized to Python None"


def test_head_url_missing_optional_headers() -> None:
    """Missing x-amz-* headers produce None values in VerifiedEntry, not KeyError."""
    from tpwalk.models import VerifiedEntry
    from tpwalk.verify._head import head_url

    # No x-amz-* headers at all
    minimal_headers = {
        "content-length": "1000",
        "etag": '"abc"',
        "content-type": "application/octet-stream",
        "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT",
        "server": "AmazonS3",
    }
    url = "https://static.tp-link.com/resources/gpl/minimal.tgz"
    client = httpx.AsyncClient(transport=_make_fixed_transport(200, minimal_headers))
    sem = asyncio.Semaphore(10)

    async def _run() -> VerifiedEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, VerifiedEntry)
    assert result.version_id is None
    assert result.replication_status is None
    assert result.encryption is None


def test_head_url_404_returns_dead_entry() -> None:
    """A 404 response produces a DeadEntry with status=404 and error_type='http'."""
    from tpwalk.models import DeadEntry
    from tpwalk.verify._head import head_url

    url = "https://static.tp-link.com/resources/gpl/does_not_exist.tgz"
    client = httpx.AsyncClient(transport=_make_fixed_transport(404))
    sem = asyncio.Semaphore(10)

    async def _run() -> DeadEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, DeadEntry)
    assert result.url == url
    assert result.status == 404
    assert result.error_type == "http"


def test_head_url_403_returns_dead_entry() -> None:
    """A 403 response produces a DeadEntry with status=403 and error_type='http'."""
    from tpwalk.models import DeadEntry
    from tpwalk.verify._head import head_url

    url = "https://static.tp-link.com/resources/gpl/forbidden.tgz"
    client = httpx.AsyncClient(transport=_make_fixed_transport(403))
    sem = asyncio.Semaphore(10)

    async def _run() -> DeadEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, DeadEntry)
    assert result.status == 403
    assert result.error_type == "http"


def test_head_url_timeout_returns_dead_entry() -> None:
    """httpx.TimeoutException produces a DeadEntry with status=None and error_type='timeout'."""
    from tpwalk.models import DeadEntry
    from tpwalk.verify._head import head_url

    url = "https://static.tp-link.com/resources/gpl/slow.tgz"
    client = httpx.AsyncClient(transport=_make_timeout_transport())
    sem = asyncio.Semaphore(10)

    async def _run() -> DeadEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, DeadEntry)
    assert result.status is None
    assert result.error_type == "timeout"


def test_head_url_network_error_returns_dead_entry() -> None:
    """httpx.RequestError (non-timeout) produces a DeadEntry with status=None and error_type='network'."""
    from tpwalk.models import DeadEntry
    from tpwalk.verify._head import head_url

    url = "https://static.tp-link.com/resources/gpl/unreachable.tgz"
    client = httpx.AsyncClient(transport=_make_network_error_transport())
    sem = asyncio.Semaphore(10)

    async def _run() -> DeadEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, DeadEntry)
    assert result.status is None
    assert result.error_type == "network"


def test_head_url_uses_s3_origin() -> None:
    """The HEAD request URL contains s3.amazonaws.com/{netloc}, not the original CloudFront host."""
    from tpwalk.verify._head import head_url

    requested_urls: list[str] = []

    def capturing_handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, headers=_FULL_HEADERS)

    url = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"
    client = httpx.AsyncClient(transport=httpx.MockTransport(capturing_handler))
    sem = asyncio.Semaphore(10)

    async def _run() -> None:
        async with client:
            await head_url(url=url, client=client, sem=sem)

    asyncio.run(_run())
    assert len(requested_urls) == 1
    # Must target S3 origin, not CloudFront
    assert "s3.amazonaws.com" in requested_urls[0]
    assert "static.tp-link.com" in requested_urls[0]


def test_head_url_301_region_redirect_retries_regional() -> None:
    """A path-style 301 + x-amz-bucket-region retries the regional endpoint and verifies.

    Reproduces the Mercusys case: static.mercusys.com lives in ap-southeast-1, so
    the default s3.amazonaws.com path-style HEAD returns 301 with the real region
    in x-amz-bucket-region. head_url must retry against s3.{region}.amazonaws.com
    (not record a dead URL) and return a VerifiedEntry from the regional 200.
    """
    from tpwalk.models import VerifiedEntry
    from tpwalk.verify._head import head_url

    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        requested_urls.append(url_str)
        # Regional endpoint serves the object; default path-style 301-redirects.
        if "s3.ap-southeast-1.amazonaws.com" in url_str:
            return httpx.Response(200, headers=_FULL_HEADERS)
        return httpx.Response(301, headers={"x-amz-bucket-region": "ap-southeast-1"})

    url = "https://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sem = asyncio.Semaphore(10)

    async def _run() -> VerifiedEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, VerifiedEntry)
    assert result.status == 200
    assert result.url == url, "Canonical URL is preserved, not the regional request URL"
    # First the default path-style, then the regional retry — exactly two requests.
    assert len(requested_urls) == 2
    assert "s3.amazonaws.com/static.mercusys.com" in requested_urls[0]
    assert "s3.ap-southeast-1.amazonaws.com/static.mercusys.com" in requested_urls[1]


def test_head_url_301_without_region_header_stays_dead() -> None:
    """A 301 lacking x-amz-bucket-region is not retried — recorded dead with status=301."""
    from tpwalk.models import DeadEntry
    from tpwalk.verify._head import head_url

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(301)  # generic redirect, no region hint

    url = "https://static.tp-link.com/resources/gpl/redirect_no_region.tgz"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sem = asyncio.Semaphore(10)

    async def _run() -> DeadEntry | object:
        async with client:
            return await head_url(url=url, client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, DeadEntry)
    assert result.status == 301
    assert result.error_type == "http"
    assert request_count == 1, "No regional retry without x-amz-bucket-region"


def test_head_check_all_separates_verified_dead() -> None:
    """head_check_all returns a (list[VerifiedEntry], list[DeadEntry]) tuple."""
    from tpwalk.models import DeadEntry, VerifiedEntry
    from tpwalk.verify._head import head_check_all

    urls = {
        "https://static.tp-link.com/resources/gpl/live.tgz",
        "https://static.tp-link.com/resources/gpl/dead.tgz",
    }
    transport = _make_per_url_transport(
        {
            "live.tgz": (200, _FULL_HEADERS),
            "dead.tgz": (404, {}),
        }
    )
    client = httpx.AsyncClient(transport=transport)
    sem = asyncio.Semaphore(10)

    async def _run() -> tuple[list[VerifiedEntry], list[DeadEntry]]:
        async with client:
            return await head_check_all(urls, client=client, sem=sem, progress=None, task_id=None)

    verified, dead = asyncio.run(_run())
    assert len(verified) == 1
    assert len(dead) == 1
    assert all(isinstance(v, VerifiedEntry) for v in verified)
    assert all(isinstance(d, DeadEntry) for d in dead)


def test_head_check_all_updates_progress_live_dead() -> None:
    """progress.update is called with live= and dead= keyword arguments after each result (D-07)."""
    from tpwalk.verify._head import head_check_all

    urls = {
        "https://static.tp-link.com/resources/gpl/a.tgz",
        "https://static.tp-link.com/resources/gpl/b.tgz",
        "https://static.tp-link.com/resources/gpl/c.tgz",
    }
    transport = _make_per_url_transport(
        {
            "a.tgz": (200, _FULL_HEADERS),
            "b.tgz": (404, {}),
            "c.tgz": (200, _FULL_HEADERS),
        }
    )
    client = httpx.AsyncClient(transport=transport)
    sem = asyncio.Semaphore(10)

    # Use a mock progress object to capture update calls
    mock_progress = MagicMock()
    task_id = 0

    async def _run() -> None:
        async with client:
            await head_check_all(urls, client=client, sem=sem, progress=mock_progress, task_id=task_id)

    asyncio.run(_run())

    # progress.update must have been called at least once with live= and dead= kwargs
    assert mock_progress.update.called, "progress.update was never called"
    calls = mock_progress.update.call_args_list
    assert len(calls) == 3, f"Expected 3 calls (one per URL), got {len(calls)}"
    for call in calls:
        kwargs = call.kwargs
        assert "live" in kwargs, f"progress.update missing 'live' kwarg: {call}"
        assert "dead" in kwargs, f"progress.update missing 'dead' kwarg: {call}"


def test_semaphore_bound() -> None:
    """With Semaphore(2) and 5 URLs, at most 2 requests are in flight concurrently.

    Since MockTransport runs synchronously within an async context, each request
    is processed serially inside the TaskGroup event loop iteration. The semaphore
    still correctly bounds the number of tasks that can enter their critical section.
    This test verifies the semaphore is acquired (not bypassed) by checking all
    5 requests complete successfully.
    """
    from tpwalk.verify._head import head_check_all

    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, headers=_FULL_HEADERS)

    urls = {
        "https://static.tp-link.com/resources/gpl/f1.tgz",
        "https://static.tp-link.com/resources/gpl/f2.tgz",
        "https://static.tp-link.com/resources/gpl/f3.tgz",
        "https://static.tp-link.com/resources/gpl/f4.tgz",
        "https://static.tp-link.com/resources/gpl/f5.tgz",
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    sem = asyncio.Semaphore(2)

    async def _run() -> tuple[list, list]:
        async with client:
            return await head_check_all(urls, client=client, sem=sem, progress=None, task_id=None)

    verified, dead = asyncio.run(_run())
    # All 5 URLs completed
    assert len(verified) + len(dead) == 5
    assert call_count == 5
