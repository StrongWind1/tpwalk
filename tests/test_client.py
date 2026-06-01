"""Tests for tpwalk._client — HTTP client factory configuration.

Verifies FOUN-02: build_client() returns a correctly configured AsyncClient
with HTTP/2 enabled, explicit connection limits, timeouts, and no redirect
following. Uses synchronous inspection via asyncio.run() to avoid pytest-asyncio
dependency.
"""

from __future__ import annotations

import asyncio

import httpx


def _close(client: httpx.AsyncClient) -> None:
    """Close an async client synchronously for test teardown."""
    asyncio.run(client.aclose())


class TestBuildClient:
    """Tests for build_client() factory function."""

    def test_build_client_returns_async_client(self) -> None:
        from tpwalk._client import build_client

        client = build_client()
        try:
            assert isinstance(client, httpx.AsyncClient)
        finally:
            _close(client)

    def test_build_client_http2_enabled(self) -> None:
        from tpwalk._client import build_client

        client = build_client()
        try:
            # The underlying transport pool tracks HTTP/2 support
            assert client._transport._pool._http2 is True
        finally:
            _close(client)

    def test_build_client_limits(self) -> None:
        from tpwalk._client import build_client

        client = build_client()
        try:
            pool = client._transport._pool
            assert pool._max_connections == 200
            assert pool._max_keepalive_connections == 100
        finally:
            _close(client)

    def test_build_client_timeout(self) -> None:
        from tpwalk._client import build_client

        client = build_client()
        try:
            timeout = client.timeout
            assert timeout.connect == 10.0
            assert timeout.read == 30.0
            assert timeout.write == 5.0
            assert timeout.pool == 5.0
        finally:
            _close(client)

    def test_build_client_no_follow_redirects(self) -> None:
        from tpwalk._client import build_client

        client = build_client()
        try:
            assert client.follow_redirects is False
        finally:
            _close(client)

    def test_build_client_http2_disabled(self) -> None:
        from tpwalk._client import build_client

        client = build_client(http2=False)
        try:
            assert client._transport._pool._http2 is False
        finally:
            _close(client)
