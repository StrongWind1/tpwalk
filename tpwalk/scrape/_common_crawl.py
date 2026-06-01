"""Common Crawl Index API source for the tpwalk scrape pipeline.

Queries every historical Common Crawl index to recover GPL archive URLs
from the static.tp-link.com and static.mercusys.com S3 buckets. Indices
are discovered dynamically at runtime from collinfo.json (D-05) and ALL
are queried without exception (D-06).

Response format is NDJSON (one JSON object per line). Each record has
many fields; only the "url" field is extracted (SCRP-12, Pitfall 8).

HTTP 404 from CC means "No Captures found" -- expected, not an error
(Pitfall 4 in RESEARCH.md).

Per SCRP-11, SCRP-12, FOUN-03, D-01, D-02, D-05, D-06, D-07, D-08.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# --- Configuration ---

_COLLINFO_URL: str = "https://index.commoncrawl.org/collinfo.json"
_HTTP_OK = 200
_HTTP_NOT_FOUND = 404

# Query prefixes per D-01, D-02: broad wildcard for TP-Link, scoped for Mercusys.
_CC_QUERY_PREFIXES: tuple[str, ...] = (
    "static.tp-link.com/*",  # D-01: broad wildcard for all static.tp-link.com paths
    "static.mercusys.com/gpl/*",  # D-02: Mercusys sub-brand GPL files
)

# Circuit breaker for the index host. index.commoncrawl.org sheds load by first
# 503-ing, then refusing connections outright ("All connection attempts failed").
# Once the host stops accepting connections, every remaining query is doomed:
# grinding ~100 indices x 2 prefixes x 4 retries only hammers a host that has
# already cut us off (and risks a longer block). After this many CONSECUTIVE
# transport failures -- timeouts / connection errors, NOT HTTP 503 (which is a
# real response, retryable, and often recovers) -- the breaker trips and every
# remaining query short-circuits to None with a single summary warning.
_CC_CIRCUIT_LIMIT: int = 10

# Politeness jitter before each index request. index.commoncrawl.org rate-limits
# bursts from a single IP; a small randomized pause spaces requests out and keeps
# concurrent workers from resynchronizing. No-op'd in tests (conftest).
_JITTER_MIN_S: float = 0.1
_JITTER_MAX_S: float = 0.4


@dataclass
class _CircuitState:
    """Mutable breaker state shared by every _fetch_with_retry call in one run.

    asyncio is single-threaded, so the plain int/bool need no lock: they are only
    read and mutated between awaits, never preempted mid-update. One instance is
    created per CommonCrawlSource.run() and threaded through every fetch.
    """

    consecutive_transport_failures: int = 0
    tripped: bool = False


def _record_transport_failure(state: _CircuitState) -> None:
    """Count a transport failure and trip the breaker at the threshold (logs once)."""
    state.consecutive_transport_failures += 1
    if not state.tripped and state.consecutive_transport_failures >= _CC_CIRCUIT_LIMIT:
        state.tripped = True
        _log.warning(
            "Common Crawl index unreachable after %d consecutive connection failures; skipping remaining queries (the host is refusing connections).",
            _CC_CIRCUIT_LIMIT,
        )


def _parse_ndjson_urls(text: str, cdx_api: str, url_prefix: str) -> set[str]:
    """Extract URL strings from a Common Crawl NDJSON response body.

    Each line is a JSON object with many fields. Only the "url" field is
    extracted (SCRP-12, Pitfall 8). Malformed lines are skipped gracefully
    (per T-03-05).

    Args:
        text: Raw response body (newline-delimited JSON).
        cdx_api: Index endpoint URL, used only for logging.
        url_prefix: Query prefix, used only for logging.

    Returns:
        Set of extracted URL strings (empty strings excluded).

    """
    urls: set[str] = set()
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            _log.debug("CC index %s: skipping malformed NDJSON line for %s", cdx_api, url_prefix)
            continue

        url = record.get("url", "") if isinstance(record, dict) else ""
        if url:
            urls.add(url)

    return urls


class CommonCrawlSource:
    """Common Crawl Index API source for tpwalk scrape pipeline.

    Discovers all available CC indices at runtime via collinfo.json (D-05),
    then queries every index for static.tp-link.com/* and static.mercusys.com/gpl/*
    using page-based pagination. Returns the union of all discovered URLs.

    Conforms to the ScrapeSource Protocol (D-11). The name property is used as
    the output filename stem by ScrapeRunner (D-14).

    Per SCRP-11, SCRP-12, D-01, D-02, D-05, D-06, D-07, D-08.
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'common_crawl.txt'."""
        return "common_crawl"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
    ) -> set[str]:
        """Execute Common Crawl index queries across all indices and return discovered URLs.

        Pipeline:
        1. Fetch collinfo.json to discover all CC indices (D-05)
        2. For each index, query both URL prefixes (D-01, D-02) with page-based pagination
        3. Union all results across all indices and prefixes

        Per D-06: ALL discovered indices are queried, no exceptions.
        Per D-08: no filtering -- all URLs from CC are kept.

        Args:
            client: Shared AsyncClient (HTTP/2 connection reuse).
            progress: Rich Progress instance for live display, or None.
            task_pass1: Rich task ID for progress bar (used for index progress), or None.
            task_pass2: Unused by this source (regional-crawl-specific). Ignored.

        Returns:
            Set of all discovered URLs (raw, not normalized -- ScrapeRunner handles that).

        """
        sem = asyncio.Semaphore(3)  # FOUN-03: CDX APIs limited to 3 concurrent
        state = _CircuitState()  # one breaker shared across all queries this run

        # Step 1: Discover indices from collinfo.json
        indices = await self._discover_indices(client=client, sem=sem, state=state)
        if not indices:
            _log.warning("Common Crawl: no indices discovered from collinfo.json")
            return set()

        _log.info("Common Crawl: discovered %d indices", len(indices))

        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, total=len(indices) * len(_CC_QUERY_PREFIXES))

        # Step 2: Query each index for each prefix using TaskGroup + Semaphore
        all_urls: set[str] = set()

        async with asyncio.TaskGroup() as tg:
            for index_entry in indices:
                cdx_api = index_entry.get("cdx-api")
                if not cdx_api:
                    _log.debug("Skipping index without cdx-api key: %s", index_entry.get("id", "unknown"))
                    continue

                for url_prefix in _CC_QUERY_PREFIXES:

                    async def _query_and_collect(
                        cdx_api: str = cdx_api,
                        url_prefix: str = url_prefix,
                    ) -> None:
                        urls = await self._query_one_index(
                            client=client,
                            sem=sem,
                            cdx_api=cdx_api,
                            url_prefix=url_prefix,
                            state=state,
                        )
                        all_urls.update(urls)

                        if progress is not None and task_pass1 is not None:
                            progress.update(task_pass1, advance=1)

                    tg.create_task(_query_and_collect())

        _log.info("Common Crawl total: %d unique URLs from %d indices", len(all_urls), len(indices))
        return all_urls

    async def _discover_indices(
        self,
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        state: _CircuitState,
    ) -> list[dict[str, str]]:
        """Fetch collinfo.json and return the list of index entries.

        Per D-05: dynamic discovery at runtime, never hardcoded.
        Per T-03-07: validate that each entry has a "cdx-api" key.

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.
            state: Shared circuit-breaker state for this run.

        Returns:
            List of index entry dicts (each with "id", "cdx-api", etc.),
            or empty list on failure.

        """
        response = await _fetch_with_retry(client=client, sem=sem, url=_COLLINFO_URL, params={}, state=state)
        if response is None or response.status_code != _HTTP_OK:
            _log.error("Common Crawl: failed to fetch collinfo.json")
            return []

        try:
            data = response.json()
        except json.JSONDecodeError, ValueError:
            _log.error("Common Crawl: malformed collinfo.json response")
            return []

        if not isinstance(data, list):
            _log.error("Common Crawl: collinfo.json is not a list")
            return []

        return data  # type: ignore[return-value]

    async def _query_one_index(
        self,
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        cdx_api: str,
        url_prefix: str,
        state: _CircuitState,
    ) -> set[str]:
        """Query one CC index for one URL prefix, paginating through all pages.

        Step 1: Discover page count via showNumPages=true.
        Step 2: For each page 0..N-1: fetch NDJSON, extract record.get("url", "").

        HTTP 404 = no captures for this prefix in this index (expected, not error).
        Per T-03-05: malformed NDJSON lines are skipped (JSONDecodeError caught per-line).

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.
            cdx_api: The index's CDX API endpoint URL.
            url_prefix: URL prefix to query for (e.g., "static.tp-link.com/*").
            state: Shared circuit-breaker state for this run.

        Returns:
            Set of extracted URL strings from this index+prefix combination.

        """
        urls: set[str] = set()

        # Step 1: discover page count
        response = await _fetch_with_retry(
            client=client,
            sem=sem,
            url=cdx_api,
            params={"url": url_prefix, "showNumPages": "true"},
            state=state,
        )

        if response is None:
            return urls

        if response.status_code == _HTTP_NOT_FOUND:
            return urls  # no captures -- expected per Pitfall 4

        if response.status_code != _HTTP_OK:
            return urls

        try:
            page_info = response.json()
        except json.JSONDecodeError, ValueError:
            _log.debug("CC index %s: malformed showNumPages response for %s", cdx_api, url_prefix)
            return urls

        num_pages = page_info.get("pages", 0) if isinstance(page_info, dict) else 0
        if num_pages == 0:
            return urls

        # Step 2: fetch each page
        for page in range(num_pages):
            page_response = await _fetch_with_retry(
                client=client,
                sem=sem,
                url=cdx_api,
                params={
                    "url": url_prefix,
                    "output": "json",
                    "page": str(page),
                },
                state=state,
            )
            if page_response is None or page_response.status_code != _HTTP_OK:
                continue

            text = page_response.text.strip()
            if text:
                urls |= _parse_ndjson_urls(text, cdx_api, url_prefix)

        return urls


async def _fetch_with_retry(  # noqa: PLR0913 -- mirrors the _wayback/_reddit fetch-helper shape; state is the breaker hook
    *,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    params: dict[str, str],
    state: _CircuitState,
    max_retries: int = 3,
) -> httpx.Response | None:
    """Fetch URL with retry, exponential backoff, and a shared circuit breaker.

    For each attempt: if the breaker has already tripped, short-circuit to None
    (the index host has cut us off; do not pile on). Otherwise acquire the
    semaphore and issue GET. If status 200 or 404, return immediately (404 = no
    captures, not an error). An HTTP status failure (e.g. 503) is a real response
    -- it resets the transport-failure streak and is retried. A timeout or
    connection error is a transport failure: it is counted toward the breaker,
    and once _CC_CIRCUIT_LIMIT consecutive transport failures accumulate the
    breaker trips so every remaining query returns None at once.

    Per-attempt failures log at DEBUG (the breaker emits the single operator-
    facing WARNING), which keeps a degraded CC run from flooding the console.

    Duplicated from _wayback.py -- acceptable for two files per plan's note.
    If a third CDX source were added, extraction to a shared module would be warranted.

    Args:
        client: Shared AsyncClient.
        sem: Semaphore bounding concurrent requests.
        url: CDX API endpoint URL.
        params: Query parameters for the request.
        state: Shared circuit-breaker state for this run.
        max_retries: Maximum number of retries (default 3).

    Returns:
        httpx.Response on success (200 or 404), or None after exhausting retries
        or once the breaker has tripped.

    """
    for attempt in range(max_retries + 1):
        if state.tripped:
            return None  # host already refusing connections; do not pile on
        try:
            async with sem:
                if state.tripped:
                    return None  # re-check after queueing on the sem so tripped tasks drain instantly
                # Politeness jitter inside the semaphore slot spaces real requests
                # out against the index host's burst throttle (no-op'd in tests).
                await asyncio.sleep(random.uniform(_JITTER_MIN_S, _JITTER_MAX_S))  # noqa: S311 -- jitter, not cryptographic
                r = await client.get(url, params=params)
            if r.status_code in {_HTTP_OK, _HTTP_NOT_FOUND}:
                state.consecutive_transport_failures = 0
                return r
            # A status response means the host is still talking to us: retryable,
            # and it clears the transport-failure streak.
            state.consecutive_transport_failures = 0
            _log.debug("CC %s returned HTTP %s (attempt %d/%d)", params.get("url", url), r.status_code, attempt + 1, max_retries + 1)
        except httpx.TimeoutException:
            _log.debug("CC %s timed out (attempt %d/%d)", params.get("url", url), attempt + 1, max_retries + 1)
            _record_transport_failure(state)
        except httpx.RequestError as err:
            _log.debug("CC %s failed: %s (attempt %d/%d)", params.get("url", url), err, attempt + 1, max_retries + 1)
            _record_transport_failure(state)

        if attempt < max_retries and not state.tripped:
            await asyncio.sleep(2.0 * (2**attempt))  # 2s, 4s, 8s per D-07

    return None
