"""Tests for tpwalk.bruteforce._head — existence-only HEAD filter with mocked HTTP.

Tests verify that exists_url returns True for HTTP 200, False for all non-200
responses and transport errors, that it targets the S3 origin URL (not
CloudFront), and that the caller-supplied semaphore bounds concurrency.

Every test uses httpx.MockTransport — no real network calls. Brute-force
volume against real S3 risks an IP ban (hard project rule).

Per BRUT-04, FOUN-03.
"""

from __future__ import annotations

import asyncio

import httpx

# --- MockTransport helper functions ---
# Identical helpers to tests/test_verify_head.py — exists_url needs the same
# response scenarios (200, 404, timeout, network error).


def _make_fixed_transport(status_code: int, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    """Return a transport that always responds with the given status and headers."""
    fixed_headers = headers or {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers=fixed_headers)

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


# Canonical test URL — a well-known legacy GPL archive.
_TEST_URL = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"


# --- Tests for exists_url ---


def test_exists_url_200_returns_true() -> None:
    """A mocked S3-origin HEAD that responds 200 causes exists_url to return True."""
    from tpwalk.bruteforce._head import exists_url

    client = httpx.AsyncClient(transport=_make_fixed_transport(200))
    sem = asyncio.Semaphore(10)

    async def _run() -> bool:
        async with client:
            return await exists_url(url=_TEST_URL, client=client, sem=sem)

    assert asyncio.run(_run()) is True


def test_exists_url_404_returns_false() -> None:
    """A mocked 404 response causes exists_url to return False."""
    from tpwalk.bruteforce._head import exists_url

    client = httpx.AsyncClient(transport=_make_fixed_transport(404))
    sem = asyncio.Semaphore(10)

    async def _run() -> bool:
        async with client:
            return await exists_url(url=_TEST_URL, client=client, sem=sem)

    assert asyncio.run(_run()) is False


def test_exists_url_403_returns_false() -> None:
    """A mocked 403 response causes exists_url to return False."""
    from tpwalk.bruteforce._head import exists_url

    client = httpx.AsyncClient(transport=_make_fixed_transport(403))
    sem = asyncio.Semaphore(10)

    async def _run() -> bool:
        async with client:
            return await exists_url(url=_TEST_URL, client=client, sem=sem)

    assert asyncio.run(_run()) is False


def test_exists_url_timeout_returns_false() -> None:
    """httpx.TimeoutException is caught (before RequestError) and returns False."""
    from tpwalk.bruteforce._head import exists_url

    client = httpx.AsyncClient(transport=_make_timeout_transport())
    sem = asyncio.Semaphore(10)

    async def _run() -> bool:
        async with client:
            return await exists_url(url=_TEST_URL, client=client, sem=sem)

    assert asyncio.run(_run()) is False


def test_exists_url_network_error_returns_false() -> None:
    """httpx.ConnectError (a RequestError subclass) is caught and returns False."""
    from tpwalk.bruteforce._head import exists_url

    client = httpx.AsyncClient(transport=_make_network_error_transport())
    sem = asyncio.Semaphore(10)

    async def _run() -> bool:
        async with client:
            return await exists_url(url=_TEST_URL, client=client, sem=sem)

    assert asyncio.run(_run()) is False


def test_exists_url_s3_origin_targeting() -> None:
    """exists_url issues its HEAD against s3.amazonaws.com/{bucket}, not the CloudFront host.

    Asserts that the actual request URL contains both "s3.amazonaws.com" and
    "static.tp-link.com", proving to_s3_origin_url is applied before the request.
    """
    from tpwalk.bruteforce._head import exists_url

    requested_urls: list[str] = []

    def capturing_handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(capturing_handler))
    sem = asyncio.Semaphore(10)

    async def _run() -> bool:
        async with client:
            return await exists_url(url=_TEST_URL, client=client, sem=sem)

    asyncio.run(_run())
    assert len(requested_urls) == 1
    # Must target S3 origin, not CloudFront CDN
    assert "s3.amazonaws.com" in requested_urls[0]
    assert "static.tp-link.com" in requested_urls[0]


def test_exists_url_semaphore_bounds_concurrency() -> None:
    """With Semaphore(1) and two concurrent exists_url calls, at most 1 is in the critical section.

    A handler that tracks concurrent in-flight requests verifies the semaphore
    is actually acquired — no more than 1 request occupies the critical section
    at any time (FOUN-03 bounded concurrency).
    """
    from tpwalk.bruteforce._head import exists_url

    max_inflight = 0
    current_inflight = 0

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        nonlocal max_inflight, current_inflight
        current_inflight += 1
        max_inflight = max(max_inflight, current_inflight)
        current_inflight -= 1
        return httpx.Response(200)

    sem = asyncio.Semaphore(1)

    async def _run() -> None:
        # Two concurrent exists_url calls share the same Semaphore(1).
        url1 = "https://static.tp-link.com/resources/gpl/A.tgz"
        url2 = "https://static.tp-link.com/resources/gpl/B.tgz"
        async with httpx.AsyncClient(transport=httpx.MockTransport(tracking_handler)) as client:
            await asyncio.gather(
                exists_url(url=url1, client=client, sem=sem),
                exists_url(url=url2, client=client, sem=sem),
            )

    asyncio.run(_run())
    assert max_inflight <= 1, f"Semaphore(1) violated: {max_inflight} concurrent requests observed"
