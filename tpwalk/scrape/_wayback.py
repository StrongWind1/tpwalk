"""Wayback CDX API source for the tpwalk scrape pipeline.

Queries web.archive.org/cdx/search/cdx across multiple URL prefixes
to recover historical GPL archive URLs that no longer appear on any
live TP-Link page. Uses resumeKey-based pagination with client-side
deduplication via set[str].

Response format (verified live):
  [["original"],              // header row (always first)
  ["https://static.tp-link.com/resources/gpl/file.tgz"],
  [],                         // empty array = delimiter before resume key
  ["eJxLzs_VKSnQzcnMy9Yp..."]]  // resume key (base64-ish opaque string)

Per SCRP-06, SCRP-07, FOUN-03, D-01, D-02, D-04, D-07, D-08, D-09.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# --- CDX query configuration ---

_CDX_ENDPOINT: str = "https://web.archive.org/cdx/search/cdx"

# CDX query prefixes per D-01, D-02, D-04.
#
# OBSERVED DEVIATION (2026-05-31): D-01/D-04 also specified the broad superset
# prefix "static.tp-link.com/*" for belt-and-suspenders coverage. It is removed
# here. The CDX server cannot enumerate the entire host keyspace within any
# practical read timeout, so the broad prefix reliably times out (all retries,
# ~134 s) while contributing ZERO URLs -- and the burst it creates makes
# web.archive.org throttle the specific prefixes that DO return data. The three
# specific TP-Link paths below (legacy flat, date-hierarchical, bare-year) plus
# regex cover the real GPL archive locations; the superset added only cost and risk.
_HTTP_OK = 200

_CDX_PREFIXES: tuple[str, ...] = (
    "static.tp-link.com/resources/gpl/*",  # D-01: legacy flat path
    "static.tp-link.com/upload/gpl-code/*",  # D-01: current date-hierarchical path
    "static.tp-link.com/20*",  # D-04: bare-year prefix outliers (2013-2026)
    "static.mercusys.com/gpl/*",  # D-02: Mercusys sub-brand
)

# Politeness jitter before each CDX request. web.archive.org throttles bursts from
# a single IP, so a small randomized pause spaces requests out and keeps concurrent
# workers from resynchronizing into a thundering herd. No-op'd in tests (conftest).
_JITTER_MIN_S: float = 0.1
_JITTER_MAX_S: float = 0.4


def _parse_cdx_page(lines: list[str], url_prefix: str) -> tuple[set[str], str | None]:
    """Parse a single CDX JSON response page into URLs and an optional resume key.

    CDX responses with output=json are line-delimited JSON arrays (NOT a single
    JSON array -- see Pitfall 5 in RESEARCH.md). Each line is independently
    parseable via json.loads(line).

    Format:
      ["original"]           <- header (skipped)
      ["https://example.com/file.tar.gz"]  <- data rows
      []                     <- empty array = delimiter before resume key
      ["eJxLzs_VKSn..."]    <- resume key (opaque string)

    Per T-03-01: malformed lines are skipped gracefully (JSONDecodeError caught per-line).

    Args:
        lines: Response body split into lines (already stripped).
        url_prefix: CDX url= parameter value, used only for logging.

    Returns:
        (urls, resume_key): set of extracted URL strings and the resume key
        string if present, or None if this is the last page.

    """
    urls: set[str] = set()
    resume_key: str | None = None
    found_delimiter = False

    for i, line in enumerate(lines):
        if i == 0:
            continue  # skip header row ["original"]

        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            _log.debug("Skipping malformed CDX line for %s: %r", url_prefix, line)
            continue

        if parsed == []:
            found_delimiter = True
            continue

        if found_delimiter:
            resume_key = parsed[0] if parsed else None
            break

        if parsed and parsed[0]:
            urls.add(parsed[0])

    return urls, resume_key


class WaybackSource:
    """Wayback CDX API source for tpwalk scrape pipeline.

    Queries web.archive.org/cdx/search/cdx across multiple URL prefixes
    to recover historical GPL archive URLs. Uses resumeKey-based pagination
    (not page-based, which is documented as less complete).

    Conforms to the ScrapeSource Protocol (D-11). The name property is used as
    the output filename stem by ScrapeRunner (D-14).

    Per SCRP-06, SCRP-07, D-01, D-02, D-04, D-07, D-08, D-09.
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'wayback_cdx.txt'."""
        return "wayback_cdx"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
    ) -> set[str]:
        """Execute Wayback CDX queries across all prefixes and return discovered URLs.

        Queries all 5 CDX prefixes sequentially (each prefix may paginate internally).
        Results from all prefixes are unioned into a single set for natural dedup.
        Per D-08: no filtering -- all URLs from CDX are kept. Per D-09: only the
        original URL field is extracted (fl=original).

        Args:
            client: Shared AsyncClient (HTTP/2 connection reuse).
            progress: Rich Progress instance for live display, or None.
            task_pass1: Rich task ID for progress bar (used for prefix progress), or None.
            task_pass2: Unused by this source (regional-crawl-specific). Ignored.

        Returns:
            Set of all discovered URLs (raw, not normalized -- ScrapeRunner handles that).

        """
        sem = asyncio.Semaphore(3)  # FOUN-03: CDX APIs limited to 3 concurrent

        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, total=len(_CDX_PREFIXES))

        all_urls: set[str] = set()
        for prefix in _CDX_PREFIXES:
            urls = await self._query_wayback_prefix(client=client, sem=sem, url_prefix=prefix)
            all_urls |= urls
            _log.info("Wayback CDX prefix %s: %d URLs", prefix, len(urls))

            if progress is not None and task_pass1 is not None:
                progress.update(task_pass1, advance=1)

        _log.info("Wayback CDX total: %d unique URLs from %d prefixes", len(all_urls), len(_CDX_PREFIXES))
        return all_urls

    async def _query_wayback_prefix(
        self,
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url_prefix: str,
    ) -> set[str]:
        """Query one Wayback CDX prefix with resumeKey pagination.

        Pagination loop: parse response line-by-line with json.loads(line),
        skip header row (index 0), collect parsed[0] into set, detect empty
        [] delimiter marking resume key on next line, extract resume key,
        continue loop. If no resume key found, break.

        Per D-09: extract only the original URL field (fl=original ensures
        only that field is returned). Per D-08: add ALL URLs without filtering.

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent CDX requests (FOUN-03).
            url_prefix: CDX url= parameter value (e.g., "static.tp-link.com/*").

        Returns:
            Set of original URLs from all pages for this prefix.

        """
        urls: set[str] = set()
        resume_key: str | None = None
        base_params: dict[str, str] = {
            "url": url_prefix,
            "output": "json",
            "fl": "original",
            "limit": "10000",
            "showResumeKey": "true",
        }

        while True:
            params = {**base_params}
            if resume_key is not None:
                params["resumeKey"] = resume_key

            response = await self._fetch_with_retry(client=client, sem=sem, url=_CDX_ENDPOINT, params=params)
            if response is None or response.status_code != _HTTP_OK:
                break

            text = response.text.strip()
            if not text:
                break

            lines = text.splitlines()
            page_urls, resume_key = _parse_cdx_page(lines, url_prefix)
            urls |= page_urls

            if resume_key is None:
                break  # no more pages

        return urls

    @staticmethod
    async def _fetch_with_retry(
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
        params: dict[str, str],
        max_retries: int = 3,
    ) -> httpx.Response | None:
        """Fetch URL with retry and exponential backoff per D-07.

        For each attempt: acquire semaphore, issue GET. If status 200 or 404,
        return response immediately. Otherwise log warning. On TimeoutException
        or RequestError, log warning. If more retries remain, sleep with
        exponential backoff (2s, 4s, 8s). After exhausting retries, return None.

        Exception precedence: httpx.TimeoutException is caught before
        httpx.RequestError because TimeoutException is a subclass (per _head.py).

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.
            url: CDX API endpoint URL.
            params: Query parameters for the request.
            max_retries: Maximum number of retries (default 3).

        Returns:
            httpx.Response on success (200 or 404), or None after exhausting retries.

        """
        for attempt in range(max_retries + 1):
            try:
                async with sem:
                    # Politeness jitter inside the semaphore slot spaces real requests
                    # out against web.archive.org's burst throttle (no-op'd in tests).
                    await asyncio.sleep(random.uniform(_JITTER_MIN_S, _JITTER_MAX_S))  # noqa: S311 -- jitter, not cryptographic
                    r = await client.get(url, params=params)
                if r.status_code in {200, 404}:
                    return r  # 404 = no results, not an error
                _log.warning("CDX %s returned HTTP %s (attempt %d/%d)", params.get("url", url), r.status_code, attempt + 1, max_retries + 1)
            except httpx.TimeoutException:
                # Catch TimeoutException before RequestError -- it is a subclass.
                _log.warning("CDX %s timed out (attempt %d/%d)", params.get("url", url), attempt + 1, max_retries + 1)
            except httpx.RequestError as err:
                _log.warning("CDX %s failed: %s (attempt %d/%d)", params.get("url", url), err, attempt + 1, max_retries + 1)

            if attempt < max_retries:
                await asyncio.sleep(2.0 * (2**attempt))  # 2s, 4s, 8s per D-07

        return None
