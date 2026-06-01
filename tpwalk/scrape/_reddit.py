"""Reddit community source for the tpwalk scrape pipeline.

Searches Reddit's public unauthenticated JSON API for GPL archive URL mentions
across a site-wide query and four named subreddits (r/openwrt, r/tplink,
r/homelab, r/netsec). Extracts static.tp-link.com and static.mercusys.com URLs
from the selftext, url, and title fields of search result posts.

DOCUMENTED SPEC DEVIATION (D-11):
    SCRP-15 literally specifies "via PRAW". However, PRAW is a synchronous library
    and this codebase is fully async (httpx). The public JSON API
    (reddit.com/search.json) achieves the requirement's intent — search relevant
    subreddits for GPL archive URLs — credential-free and async-native. This
    deviation is recorded here so the divergence is traceable per the
    spec-compliance rule (SCRP-15, observed deviation: PRAW is sync; JSON API
    is async + credential-free, per D-10 and D-11).

Per SCRP-15, D-10, D-11, D-12, FOUN-03.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# --- Module constants ---

_REDDIT_BASE = "https://www.reddit.com"
_HTTP_OK = 200

# Descriptive User-Agent required on every request.
# Omitting it causes an immediate 429 from Reddit's rate limiter (Pitfall 4, SCRP-15).
_REDDIT_UA = "tpwalk/1.0 gpl-url-discovery (+https://github.com/StrongWind1/tpwalk)"

# Named subreddits per D-12, SCRP-15.
# r/openwrt, r/tplink, r/homelab, r/netsec cover the primary communities
# that discuss TP-Link firmware and GPL source code.
_SUBREDDITS: tuple[str, ...] = ("openwrt", "tplink", "homelab", "netsec")

# Anchored pattern: extracts only https://static.(tp-link|mercusys).com/ URLs.
# Anchored to known CDN domains — prevents SSRF or injection via malicious post content
# (T-04-09). Stops at whitespace, quotes, angle-brackets, and closing parens.
_GPL_URL_RE: re.Pattern[str] = re.compile(r"https://static\.(?:tp-link|mercusys)\.com/[^\s\"'<>)]+")

# Trailing punctuation stripped after extraction from free-text post fields (WR-01).
# Prose sentences end with . , ; : ! or quoted characters that the regex absorbs.
_TRAILING_PUNCT: str = ".,;:!?'\""

# Reddit listings return at most 1,000 items (limit=100 * 10 pages = 1,000).
# Match the sibling caps: Discourse uses 4 pages, GitHub uses _MAX_GITHUB_PAGES=10.
# 10 pages is a safe ceiling that mirrors the GitHub cap and the Reddit hard limit.
_MAX_REDDIT_PAGES: int = 10

# Block/deny statuses that retrying cannot clear. Reddit returns 403 (and 429) to
# datacenter IPs and to unauthenticated search regardless of how many times we ask;
# the public JSON API is simply not reachable from here. Retrying only burns time
# and deepens the rate-limit (an IP-ban risk), so these are a terminal soft-skip
# signal -- the source already degrades to an empty set (SCRP-05, D-10).
_BLOCK_STATUSES: frozenset[int] = frozenset({401, 403, 429, 451})


async def _fetch_with_retry(  # noqa: PLR0913
    *,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    max_retries: int = 3,
) -> httpx.Response | None:
    """Fetch URL with retry and exponential backoff.

    Copied from _wayback.py verbatim with the addition of a headers parameter
    so Reddit's mandatory User-Agent (Pitfall 4) can be passed on every request.

    Success set is {200, 404}: 404 means no results, not an error.
    TimeoutException is caught before RequestError because it is a subclass.
    Backoff schedule: 2s / 4s / 8s (D-07 shape reused per FOUN-03).

    Args:
        client: Shared async HTTP client.
        sem: Semaphore bounding concurrent external-API requests (FOUN-03).
        url: Target endpoint URL.
        params: Query parameters.
        headers: Per-request headers (must include User-Agent for Reddit).
        max_retries: Maximum retry attempts (default 3).

    Returns:
        httpx.Response on 200 or 404, or None after exhausting retries.

    """
    for attempt in range(max_retries + 1):
        try:
            async with sem:
                r = await client.get(url, params=params, headers=headers)
            if r.status_code in {200, 404}:
                return r
            if r.status_code in _BLOCK_STATUSES:
                # Terminal block: do not retry, do not alarm. One debug line, then
                # the caller sees None and soft-skips this endpoint.
                _log.debug("Reddit %s blocked (HTTP %s); soft-skipping without retry", url, r.status_code)
                return None
            _log.warning(
                "Reddit %s returned HTTP %s (attempt %d/%d)",
                url,
                r.status_code,
                attempt + 1,
                max_retries + 1,
            )
        except httpx.TimeoutException:
            # Catch TimeoutException before RequestError -- it is a subclass.
            _log.warning("Reddit %s timed out (attempt %d/%d)", url, attempt + 1, max_retries + 1)
        except httpx.RequestError as err:
            _log.warning("Reddit %s failed: %s (attempt %d/%d)", url, err, attempt + 1, max_retries + 1)

        if attempt < max_retries:
            await asyncio.sleep(2.0 * (2**attempt))  # 2s, 4s, 8s per D-07

    return None


class RedditSource:
    """Reddit public JSON search source for tpwalk scrape pipeline.

    Searches site-wide and four named subreddits for posts mentioning GPL
    archive URLs. Uses the public unauthenticated JSON endpoint (D-10, D-11).
    No OAuth app or PRAW required.

    Conforms to the ScrapeSource Protocol. The name property is used as the
    output filename stem by ScrapeRunner (D-14, D-18).

    Per SCRP-15, D-10, D-11, D-12, FOUN-03.
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'reddit.txt'."""
        return "reddit"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
        task_pass1: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
    ) -> set[str]:
        """Execute Reddit JSON search across site-wide + all named subreddits.

        Builds asyncio.Semaphore(3) per FOUN-03 external-API category.
        Runs a site-wide search first (D-12: GPL URLs could appear in any
        subreddit), then each of the four named subreddits with restrict_sr=1.

        Args:
            client: Shared AsyncClient (HTTP/2 connection reuse).
            progress: Rich Progress instance for live display, or None.
            task_pass1: Rich task ID for progress bar, or None.
            task_pass2: Unused by this source. Ignored.

        Returns:
            Set of all GPL archive URLs found across all queries.

        """
        sem = asyncio.Semaphore(3)  # FOUN-03: external API category
        all_urls: set[str] = set()

        # Site-wide search (D-12: covers all subreddits simultaneously)
        urls = await self._search(
            client=client,
            sem=sem,
            endpoint=f"{_REDDIT_BASE}/search.json",
            params={"q": "static.tp-link.com gpl", "limit": "100", "sort": "relevance", "t": "all"},
        )
        all_urls |= urls

        # Per-subreddit search with restrict_sr=1 (D-12, SCRP-15)
        for sub in _SUBREDDITS:
            urls = await self._search(
                client=client,
                sem=sem,
                endpoint=f"{_REDDIT_BASE}/r/{sub}/search.json",
                params={"q": "static.tp-link.com", "restrict_sr": "1", "limit": "100", "sort": "relevance", "t": "all"},
            )
            all_urls |= urls

        _log.info("Reddit total: %d URLs from site-wide + %d subreddits", len(all_urls), len(_SUBREDDITS))
        return all_urls

    async def _search(
        self,
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        endpoint: str,
        params: dict[str, str],
    ) -> set[str]:
        """Paginate a single Reddit search endpoint using the after cursor.

        Cursor loop: copies params, injects after= when set, calls _fetch_with_retry
        with the descriptive User-Agent (Pitfall 4), extracts GPL URLs from
        selftext/url/title of each child, advances the cursor from data.after,
        and stops when after is None (D-10 — null means no more pages).

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.
            endpoint: Full URL of the search.json endpoint (site-wide or per-subreddit).
            params: Base query parameters (q=, restrict_sr=, limit=, etc.).

        Returns:
            Set of GPL archive URLs extracted from all pages of this endpoint.

        """
        urls: set[str] = set()
        after: str | None = None
        seen_cursors: set[str] = set()  # cycle-detection: track every after value seen

        for _page in range(_MAX_REDDIT_PAGES):
            page_params = {**params}
            if after:
                page_params["after"] = after

            response = await _fetch_with_retry(
                client=client,
                sem=sem,
                url=endpoint,
                params=page_params,
                headers={"User-Agent": _REDDIT_UA},  # Pitfall 4: mandatory on every request
            )
            if response is None or response.status_code != _HTTP_OK:
                break

            data = response.json().get("data", {})
            children = data.get("children", [])
            if not children:
                break

            for child in children:
                post = child.get("data", {})
                for field in ("selftext", "url", "title"):
                    # Strip trailing sentence punctuation to avoid phantom URLs (WR-01).
                    urls.update(u.rstrip(_TRAILING_PUNCT) for u in _GPL_URL_RE.findall(post.get(field, "")))

            after = data.get("after")  # None when no more pages (D-10)
            if not after or after in seen_cursors:  # null cursor or cycle → stop
                break
            seen_cursors.add(after)

        return urls
