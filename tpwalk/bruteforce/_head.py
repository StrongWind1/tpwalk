"""Existence-only HEAD filter for brute-force S3 enumeration.

Only HTTP 200 means "object exists and is publicly readable". All other
statuses (403/404/5xx) and all transport errors are treated as "does not
exist". No response headers are parsed — only r.status_code is consumed.
This is the intentional thin-filter design: every brute-force candidate
(>99% of which are 404s) flows through this filter, and constructing a
full VerifiedEntry per call would be wasted work. The verify phase
re-checks every hit for full metadata (D-10).

No retry on transient errors — a timeout or 5xx is a plain miss. The
verify phase re-checks, so retrying here would only slow the sweep
(RESEARCH Pattern 1, "Why not retry").

Per BRUT-04, D-11, FOUN-03.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from tpwalk._normalize import to_s3_origin_url

if TYPE_CHECKING:
    import asyncio

# HTTP 200 is the only status that confirms an S3 object exists and is
# publicly readable. Any other status (403 "NoSuchBucket", 404, 5xx) means
# the guessed key does not exist or is inaccessible.
_HTTP_OK = 200


async def exists_url(
    *,
    url: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> bool:
    """HEAD-check one candidate URL against the S3 origin and return existence as bool.

    Converts the canonical HTTPS URL to its S3 origin form via to_s3_origin_url
    (BRUT-04, D-11), acquires the caller-supplied semaphore (FOUN-03 bounded
    concurrency), issues a non-following HEAD request, and returns True only if
    the response status is 200.

    Exception precedence: httpx.TimeoutException is caught before
    httpx.RequestError because TimeoutException is a RequestError subclass —
    catching the base class first would misclassify timeouts as generic network
    errors.

    Both timeout and network errors return False (miss) without propagating the
    exception — a transient miss is re-checked by the verify phase (D-10).

    Args:
        url: Canonical HTTPS URL to existence-check.
        client: Shared AsyncClient. Must remain open for the lifetime of the call.
        sem: Semaphore bounding concurrent HEAD requests (FOUN-03).

    Returns:
        True if the S3 origin responds with HTTP 200 (object exists).
        False for any non-200 response or any transport error.

    """
    s3_url = to_s3_origin_url(url)

    async with sem:
        try:
            r = await client.head(s3_url, follow_redirects=False)
        except httpx.TimeoutException:
            # Catch TimeoutException before RequestError — it is a subclass.
            return False
        except httpx.RequestError:
            return False

    return r.status_code == _HTTP_OK
