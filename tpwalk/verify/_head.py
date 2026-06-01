"""Async HEAD checker for the tpwalk verify pipeline.

Two public functions:
- head_url: HEAD-checks one URL against the S3 origin and returns a typed result.
- head_check_all: Runs HEAD checks concurrently for all URLs under semaphore control.

HEAD requests target s3.amazonaws.com/{host}{path} directly, bypassing CloudFront,
to receive richer metadata headers (x-amz-version-id, replication-status,
x-amz-server-side-encryption) that CloudFront strips.

Per FOUN-03, VERF-04, VERF-05, VERF-07, D-07.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from tpwalk._normalize import to_s3_origin_url, to_s3_regional_url
from tpwalk.models import DeadEntry, VerifiedEntry

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

# HTTP 200 is the only success status from S3 HEAD — all others are dead.
_HTTP_OK = 200

# S3 path-style (s3.amazonaws.com) serves only us-east-1 buckets; a bucket in
# any other region answers with 301 Moved Permanently and the real region in
# x-amz-bucket-region. We retry that one case against the regional endpoint.
_S3_REGION_REDIRECT = 301


def _null_to_none(value: str | None) -> str | None:
    """Normalize the S3 string 'null' to Python None.

    Legacy files (pre-2022, /resources/gpl/) return x-amz-version-id: null
    as a literal string, not a missing header. This collapses that form to
    Python None so VerifiedEntry fields are typed consistently.

    Per RESEARCH.md Pitfall 2 and VERF-05.
    """
    return value if value != "null" else None


async def head_url(
    *,
    url: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> VerifiedEntry | DeadEntry:
    """HEAD-check one URL against the S3 origin and return a typed result.

    Computes the S3 origin URL (bypassing CloudFront) via to_s3_origin_url,
    acquires the semaphore, and issues a HEAD request. The canonical HTTPS URL
    is preserved in the returned entry — only the request uses the S3 origin URL.

    Exception precedence: httpx.TimeoutException is caught before
    httpx.RequestError because TimeoutException is a RequestError subclass —
    catching the base class first would misclassify timeouts as generic network
    errors.

    Per RESEARCH.md Pattern 3 and FOUN-03 (semaphore-bounded concurrency).

    Args:
        url: Canonical HTTPS URL (output of url_normalize). Stored in the result.
        client: Shared AsyncClient. Must remain open for the lifetime of the call.
        sem: Semaphore bounding concurrent HEAD requests.

    Returns:
        VerifiedEntry with all D-06 fields if the response is 200.
        DeadEntry with status and error_type if the response is non-200 or errors.

    """
    checked_at = datetime.now(UTC).isoformat()
    s3_url = to_s3_origin_url(url)

    async with sem:
        try:
            r = await client.head(s3_url, follow_redirects=False)
            # Buckets outside us-east-1 (e.g. the Mercusys GPL bucket in
            # ap-southeast-1) answer the path-style endpoint with 301 +
            # x-amz-bucket-region. Retry once against the region-qualified
            # endpoint, which returns the object's metadata directly and keeps
            # the x-amz-* headers that following the CloudFront redirect strips.
            # A 301 without the region header is left as-is (recorded dead below).
            region = r.headers.get("x-amz-bucket-region")
            if r.status_code == _S3_REGION_REDIRECT and region:
                r = await client.head(to_s3_regional_url(url, region), follow_redirects=False)
        except httpx.TimeoutException:
            # Catch TimeoutException before RequestError — it is a subclass.
            # status=None signals "no response received".
            return DeadEntry(url=url, status=None, error_type="timeout", checked_at=checked_at)
        except httpx.RequestError:
            return DeadEntry(url=url, status=None, error_type="network", checked_at=checked_at)

    if r.status_code == _HTTP_OK:
        h = r.headers
        return VerifiedEntry(
            url=url,
            size=int(h.get("content-length", 0)),
            etag=h.get("etag", ""),
            content_type=h.get("content-type", ""),
            last_modified=h.get("last-modified", ""),
            # Normalize "null" string from legacy S3 files to Python None.
            version_id=_null_to_none(h.get("x-amz-version-id")),
            replication_status=_null_to_none(h.get("x-amz-replication-status")),
            encryption=_null_to_none(h.get("x-amz-server-side-encryption")),
            server=h.get("server", ""),
            status=200,
            checked_at=checked_at,
        )

    return DeadEntry(url=url, status=r.status_code, error_type="http", checked_at=checked_at)


async def head_check_all(
    urls: set[str],
    *,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    progress: Progress | None,
    task_id: TaskID | None,
) -> tuple[list[VerifiedEntry], list[DeadEntry]]:
    """Run HEAD checks for all URLs concurrently under semaphore control.

    Uses asyncio.TaskGroup (not asyncio.gather) for structured concurrency:
    TaskGroup propagates exceptions cleanly and cancels sibling tasks on failure.

    After each URL completes, updates the progress bar with the current live and
    dead counts per D-07. The progress object is optional — passing None skips
    the update (used during programmatic testing without a terminal).

    Per FOUN-03 (TaskGroup + Semaphore pattern), D-07 (live/dead counter updates).

    Args:
        urls: Normalized URL set from read_all_txt.
        client: Shared AsyncClient. Must remain open for the call's duration.
        sem: Semaphore bounding concurrent HEAD requests.
        progress: Rich Progress instance, or None to suppress updates.
        task_id: Rich task ID for the progress bar task, or None.

    Returns:
        Tuple of (verified_entries, dead_entries). Both lists are populated
        as tasks complete; order within each list is non-deterministic.

    """
    verified: list[VerifiedEntry] = []
    dead: list[DeadEntry] = []

    async def _check(url: str) -> None:
        """Inner coroutine: HEAD-check one URL and append to the correct list."""
        result = await head_url(url=url, client=client, sem=sem)
        if isinstance(result, VerifiedEntry):
            verified.append(result)
        else:
            dead.append(result)

        # Update progress with live/dead field counters per D-07.
        # advance=1 increments the progress bar; live= and dead= update the
        # custom text columns showing live/dead counts in real time.
        if progress is not None and task_id is not None:
            progress.update(task_id, advance=1, live=len(verified), dead=len(dead))

    async with asyncio.TaskGroup() as tg:
        for url in urls:
            tg.create_task(_check(url))

    return verified, dead
