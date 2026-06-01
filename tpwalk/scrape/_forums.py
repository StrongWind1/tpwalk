"""Community-forums source for the tpwalk scrape pipeline.

Implements a dual-path discovery strategy for GPL archive URLs mentioned on
community forums:

1. OpenWrt Discourse JSON search (credential-free, native): forum.openwrt.org
   exposes a public search.json endpoint with no authentication required (D-18).
   Posts include a "blurb" text excerpt field that contains the URL when it was
   mentioned in the post body. This is more robust than Google dorking for OpenWrt
   because it is less susceptible to Google anti-bot blocking.

2. Google site: dorks (D-15 baseline) for forums that block unauthenticated
   automated search:
   - forum.dd-wrt.com  -- phpBB2 with Anubis bot protection (RESEARCH §SCRP-14)
   - snbforums.com     -- XenForo, HTTP 403 on unauthenticated search
   - linksysinfo.org   -- XenForo, the FreshTomato community (Assumption A5)

All Google paths are soft sources (D-14): any block signal (429, consent wall,
"unusual traffic") returns an empty set without raising. The OpenWrt Discourse
path and each Google dork are independently isolated so one failure never loses
results from the others (SCRP-05).

Per SCRP-14, D-14, D-15, D-18, FOUN-03.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# --- Constants ---

_HTTP_OK = 200
_HTTP_TOO_MANY_REQUESTS = 429

# Block/deny statuses that retrying cannot clear. OpenWrt's Discourse rate-limits
# anonymous JSON search with 429; hammering it 4x only deepens the limit (an
# IP-ban risk). Treat these as a terminal soft-skip -- ForumSource already
# degrades to an empty set on any block (D-14, SCRP-05).
_BLOCK_STATUSES: frozenset[int] = frozenset({401, 403, 429, 451})

_OPENWRT_SEARCH = "https://forum.openwrt.org/search.json"
_GOOGLE_SEARCH = "https://www.google.com/search"

# Forums that require Google site: dork because their native search is
# blocked for unauthenticated requests (RESEARCH §SCRP-14).
# linksysinfo.org IS the FreshTomato community forum (Assumption A5).
_FORUM_DORK_DOMAINS: tuple[str, ...] = (
    "forum.dd-wrt.com",  # phpBB2 with Anubis bot protection
    "snbforums.com",  # XenForo -- 403 on unauthenticated search
    "linksysinfo.org",  # FreshTomato community (XenForo, same 403 pattern)
)

# Anchored regex matching GPL archive URLs on both tp-link and mercusys CDNs.
# Used for blurb extraction (Discourse) and regex fallback (Google SERP).
_GPL_URL_RE: re.Pattern[str] = re.compile(r"https://static\.(?:tp-link|mercusys)\.com/[^\s\"'<>)]+")

# Trailing punctuation stripped after extraction from free-text fields (WR-01).
# Post blurbs and raw SERP text often terminate URLs with . , ; : ! or quotes.
_TRAILING_PUNCT: str = ".,;:!?'\""

# Realistic browser User-Agent to reduce immediate Google blocking.
# Note: even with headers, expect blocks at low request counts (RESEARCH Pattern 5).
_GOOGLE_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# --- Module-level fetch helper (mirrors _wayback.py / _common_crawl.py pattern) ---


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
    httpx.RequestError because TimeoutException is a subclass.

    Duplicated from _wayback.py -- acceptable for two files per PATTERNS note.

    Args:
        client: Shared AsyncClient.
        sem: Semaphore bounding concurrent requests (FOUN-03).
        url: Request endpoint URL.
        params: Query parameters for the request.
        max_retries: Maximum number of retries (default 3).

    Returns:
        httpx.Response on success (200 or 404), or None after exhausting retries.

    """
    for attempt in range(max_retries + 1):
        try:
            async with sem:
                r = await client.get(url, params=params)
            if r.status_code in {200, 404}:
                return r
            if r.status_code in _BLOCK_STATUSES:
                # Terminal block: do not retry, do not alarm. The caller sees None
                # and soft-skips this path (other forum paths are unaffected).
                _log.debug("Forum %s blocked (HTTP %s); soft-skipping without retry", url, r.status_code)
                return None
            _log.warning("Forum %s returned HTTP %s (attempt %d/%d)", url, r.status_code, attempt + 1, max_retries + 1)
        except httpx.TimeoutException:
            # Catch TimeoutException before RequestError -- it is a subclass.
            _log.warning("Forum %s timed out (attempt %d/%d)", url, attempt + 1, max_retries + 1)
        except httpx.RequestError as err:
            _log.warning("Forum %s failed: %s (attempt %d/%d)", url, err, attempt + 1, max_retries + 1)

        if attempt < max_retries:
            await asyncio.sleep(2.0 * (2**attempt))  # 2s, 4s, 8s per D-07

    return None


# --- ForumSource ---


class ForumSource:
    """Community-forums source: OpenWrt Discourse JSON + Google site: dork fallback.

    Queries OpenWrt's public Discourse search.json directly (D-18: native
    credential-free search is more robust than Google dorking for this forum).
    Falls back to Google site: dorks for DD-WRT, SNBForums, and FreshTomato
    (linksysinfo.org), which all block unauthenticated automated search (D-15).

    All paths are soft: block signals return empty set without raising (D-14).
    The Discourse path and each Google dork are independently isolated so one
    failure does not lose results from the others (SCRP-05).

    Conforms to the ScrapeSource Protocol (D-11). The name property is used as
    the output filename stem by ScrapeRunner (D-14).

    Per SCRP-14, D-14, D-15, D-18, FOUN-03.
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'forums.txt'."""
        return "forums"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
        task_pass1: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
    ) -> set[str]:
        """Execute OpenWrt Discourse search and Google site: dorks for blocked forums.

        Runs the OpenWrt Discourse path and all Google dork paths concurrently
        via asyncio.TaskGroup. Each path is independently isolated: one block
        or failure does not stop the others (SCRP-05).

        Args:
            client: Shared AsyncClient (HTTP/2 connection reuse).
            progress: Rich Progress instance for live display, or None.
            task_pass1: Unused by this source. Ignored.
            task_pass2: Unused by this source. Ignored.

        Returns:
            Set of all discovered GPL archive URLs (raw, not normalized).

        """
        sem = asyncio.Semaphore(3)  # FOUN-03: external API category
        all_urls: set[str] = set()

        async with asyncio.TaskGroup() as tg:

            async def _collect_openwrt() -> None:
                urls = await self._search_openwrt(client=client, sem=sem)
                all_urls.update(urls)

            tg.create_task(_collect_openwrt())

            for domain in _FORUM_DORK_DOMAINS:

                async def _collect_dork(site: str = domain) -> None:
                    urls = await self._google_site_dork(client=client, sem=sem, site=site, query_suffix="static.tp-link.com gpl")
                    all_urls.update(urls)

                tg.create_task(_collect_dork())

        _log.info("ForumSource total: %d URLs from OpenWrt Discourse + %d Google dork domains", len(all_urls), len(_FORUM_DORK_DOMAINS))
        return all_urls

    async def _search_openwrt(self, *, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> set[str]:
        """Query OpenWrt Discourse JSON search. No auth required (verified live).

        Per RESEARCH Pattern 3: the "blurb" field in each post entry contains a
        text excerpt of the post body. If a GPL URL was mentioned in the post, it
        appears in the blurb. This avoids the need to fetch each full post
        individually (no N+1 requests).

        Pagination via ?page=N; stops when grouped_search_result.more_posts is
        false or after 4 pages maximum (Assumption A4: Discourse pagination is
        unreliable past page 2-3 and may produce duplicates).

        Per SCRP-14, D-18, RESEARCH Pattern 3, Assumption A4.

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests (FOUN-03).

        Returns:
            Set of GPL archive URLs extracted from post blurb fields.

        """
        urls: set[str] = set()
        for page in range(1, 5):  # cap at 4 pages; Discourse pagination unreliable past 3 (A4)
            response = await _fetch_with_retry(
                client=client,
                sem=sem,
                url=_OPENWRT_SEARCH,
                params={"q": "static.tp-link.com gpl", "page": str(page)},
            )
            if response is None or response.status_code != _HTTP_OK:
                break
            data = response.json()
            posts = data.get("posts", [])
            for post in posts:
                # Strip trailing sentence punctuation to avoid phantom URLs (WR-01).
                urls.update(u.rstrip(_TRAILING_PUNCT) for u in _GPL_URL_RE.findall(post.get("blurb", "")))
            # Pagination stop: more_posts=False means this is the last page
            if not data.get("grouped_search_result", {}).get("more_posts"):
                break
        return urls

    async def _google_site_dork(
        self,
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        site: str,
        query_suffix: str,
    ) -> set[str]:
        """Google site: dork for a specific forum domain. Soft source -- skip on any block.

        Sends a single Google search request with a site: operator to find GPL
        archive URLs mentioned on forums that block unauthenticated native search
        (DD-WRT, SNBForums, FreshTomato/linksysinfo.org per RESEARCH §SCRP-14).

        Block detection (Pitfall 6 from RESEARCH.md):
        - HTTP 429: rate limited; log WARNING and return empty set.
        - Response URL contains consent.google.com: consent wall hit; log WARNING
          and return empty set (not parsed as results).
        - Response body contains "unusual traffic": bot detection; log WARNING and
          return empty set.

        URL extraction uses BeautifulSoup on <a href> attributes first (RESEARCH
        Pattern 5 selector -- low confidence, may break on Google HTML changes).
        Falls back to raw regex over the full response body when the selector
        yields nothing (Assumption A2).

        NEVER raises -- all error paths return empty set (D-14).

        Per SCRP-14, D-14, D-15, Pitfall 6, RESEARCH Pattern 5, Assumption A2.

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests (FOUN-03).
            site: Forum domain to dork (e.g., "forum.dd-wrt.com").
            query_suffix: Additional query terms appended after the site: operator.

        Returns:
            Set of GPL archive URLs found via Google SERP, or empty set on any block.

        """
        params = {"q": f"site:{site} {query_suffix}", "num": "10"}
        headers = {"User-Agent": _GOOGLE_UA, "Accept-Language": "en-US,en;q=0.9"}
        try:
            async with sem:
                r = await client.get(_GOOGLE_SEARCH, params=params, headers=headers, follow_redirects=True)
        except (httpx.TimeoutException, httpx.RequestError) as err:
            _log.debug("Google dork for %s failed: %s", site, err)
            return set()

        # Block detection (Pitfall 6)
        if r.status_code == _HTTP_TOO_MANY_REQUESTS:
            _log.warning("Google blocked (429) for site:%s dork", site)
            return set()
        if "consent.google.com" in str(r.url):
            _log.warning("Google consent wall hit for site:%s dork", site)
            return set()
        if "unusual traffic" in r.text.lower():
            _log.warning("Google unusual-traffic block for site:%s dork", site)
            return set()

        # Primary selector (LOW confidence per Assumption A2 -- may break on Google HTML changes).
        # Finds <a> tags whose href contains a known GPL CDN hostname.
        soup = BeautifulSoup(r.text, "lxml")
        urls: set[str] = set()
        for tag in soup.select("a[href*='static.tp-link.com'], a[href*='static.mercusys.com']"):
            href = tag.get("href", "")
            if isinstance(href, str) and href:
                urls.add(href)

        # Fallback: regex over full HTML when selector yields nothing (Assumption A2 degradation).
        # Strip trailing punctuation to avoid phantom URLs from HTML body text (WR-01).
        if not urls:
            urls = {u.rstrip(_TRAILING_PUNCT) for u in _GPL_URL_RE.findall(r.text)}

        return urls
