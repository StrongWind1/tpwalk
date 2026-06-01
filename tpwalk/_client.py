"""HTTP client factory for tpwalk S3 origin HEAD requests.

A single AsyncClient instance is shared across all concurrent workers so HTTP/2
multiplexes all HEAD requests over one TCP connection per origin. Creating one
client per worker would open separate TCP connections and destroy HTTP/2
connection reuse — the primary performance advantage for checking 1,283+ URLs.

Per FOUN-02.
"""

from __future__ import annotations

import httpx


def build_client(*, http2: bool = True) -> httpx.AsyncClient:
    """Return a configured AsyncClient for S3 origin HEAD checks.

    A single client is shared across all concurrent workers so HTTP/2
    multiplexes all HEAD requests over one TCP connection per origin.
    Creating one client per worker would open separate TCP connections
    and destroy HTTP/2 connection reuse.

    Connection limits are sized for concurrent HEAD checks against S3 origin
    (no rate limiting observed in research). Timeouts prevent hung tasks when
    an S3 endpoint is slow or unresponsive.

    Per FOUN-02. Keyword-only arguments enforce explicit call sites.

    Args:
        http2: Enable HTTP/2 multiplexing. Disable only for debugging or when
            the remote endpoint does not support HTTP/2.

    Returns:
        Configured AsyncClient ready for use as an async context manager.

    """
    return httpx.AsyncClient(
        http2=http2,
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=100,
            keepalive_expiry=30.0,
        ),
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0),
        follow_redirects=False,
    )
