"""Google SERP source for the tpwalk scrape pipeline.

Implements a best-effort, credential-free scraper that queries Google with
``site:static.tp-link.com`` combined with each of the 8 GPL archive extensions
(SCRP-17, D-13). Extracts result hrefs from SERP HTML using BeautifulSoup on
the ``div.yuRUbf a`` selector (RESEARCH Pattern 5, LOW confidence -- Google
changes its HTML frequently) with a regex fallback for when that selector
yields nothing (Assumption A2).

DESIGN NOTE (D-13, D-14): This is deliberately a SOFT source.

``httpx`` plain GET hits Google's anti-bot systems quickly. The implementation
makes at most one page of results per extension query and skips gracefully on
any block signal (429, consent wall, "unusual traffic") -- it NEVER raises.
There is NO retry: a block is an immediate logged skip. Requests are serialized
with ``asyncio.Semaphore(1)`` to minimize CAPTCHA risk.

This accepted cost of the credential-free v1 choice is documented in D-14;
reliable Google access (paid SERP API or Custom Search JSON API) is
v2-deferred per DISC-02. Registration into ScrapeRunner._sources happens in
plan 04-06 (Wave 2 wiring).

Per SCRP-17, D-13, D-14, Pitfall 6, FOUN-03.
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

_GOOGLE_SEARCH = "https://www.google.com/search"

# Realistic browser User-Agent to reduce immediate Google blocking.
# Even with headers, expect blocks at very low request counts (RESEARCH Pattern 5).
_GOOGLE_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Archive extensions per SCRP-17 and GPL-RECON §URL Structure (8 extensions).
# Each triggers one serialized SERP query: site:static.tp-link.com {ext}.
_ARCHIVE_EXTENSIONS: tuple[str, ...] = ("tar.gz", "tar.bz2", "tar", "tgz", "zip", "rar", "gz", "bz2")

# Anchored regex matching GPL archive URLs on both tp-link and mercusys CDNs.
# Used as the primary URL extraction mechanism when BeautifulSoup selector fails (A2).
_GPL_URL_RE: re.Pattern[str] = re.compile(r"https://static\.(?:tp-link|mercusys)\.com/[^\s\"'<>)]+")

# Trailing punctuation stripped after extraction from raw HTML text (WR-01).
# SERP pages embed URLs in prose that terminates with . , ; : ! or quotes.
_TRAILING_PUNCT: str = ".,;:!?'\""


# --- GoogleSource ---


class GoogleSource:
    """Best-effort Google SERP scraper for GPL archive URLs (SCRP-17, D-13).

    Issues one ``site:static.tp-link.com {ext}`` query per archive extension
    (8 total) and extracts result hrefs from the SERP HTML. Requests are
    serialized with Semaphore(1) to minimize CAPTCHA risk (D-14).

    Any block signal (429, consent wall redirect, "unusual traffic" body) causes
    an immediate graceful skip -- the source returns whatever it has collected so
    far and never raises. There is no retry (this is a soft source, unlike the
    retrying CDX and Common Crawl sources).

    Conforms to the ScrapeSource Protocol (D-17). The name property is used as
    the output filename stem by ScrapeRunner (D-14). Registration into
    ScrapeRunner._sources happens in plan 04-06 (Wave 2 wiring).

    Per SCRP-17, D-13, D-14, Pitfall 6, FOUN-03.
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'google.txt' (D-13, D-18 stem decision)."""
        return "google"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
    ) -> set[str]:
        """Scrape Google SERP for each of the 8 GPL archive extensions.

        Iterates _ARCHIVE_EXTENSIONS sequentially (one extension per request).
        Each iteration calls _query_extension which handles all block detection
        and URL extraction. Results across all extensions are unioned into a
        single set.

        No pagination -- one page per extension is the practical maximum before
        triggering CAPTCHA (RESEARCH Pattern 5, SCRP-17 design note).

        Args:
            client: Shared AsyncClient (HTTP/2 connection reuse).
            progress: Rich Progress instance for live display, or None.
            task_pass1: Rich task ID for pass-1 progress updates, or None.
            task_pass2: Unused by this source (single-pass). Ignored.

        Returns:
            Set of all discovered GPL archive URLs (raw, not normalized).

        """
        # Serialize ALL requests through a single-slot semaphore to minimize
        # the number of concurrent Google requests -- Google blocks aggressively
        # on concurrent or rapid-fire requests (RESEARCH Pattern 5, Pitfall 6).
        sem = asyncio.Semaphore(1)

        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, total=len(_ARCHIVE_EXTENSIONS))

        all_urls: set[str] = set()
        for ext in _ARCHIVE_EXTENSIONS:
            urls = await self._query_extension(client=client, sem=sem, ext=ext)
            all_urls |= urls
            if progress is not None and task_pass1 is not None:
                progress.update(task_pass1, advance=1)

        _log.info("Google SERP: %d URLs from %d extension queries", len(all_urls), len(_ARCHIVE_EXTENSIONS))
        return all_urls

    async def _query_extension(
        self,
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        ext: str,
    ) -> set[str]:
        """Fetch one Google SERP page for site:static.tp-link.com + extension.

        Soft source (D-14): any block signal returns empty set immediately.
        Never raises -- all error paths return set().

        Block detection (Pitfall 6 from RESEARCH.md):
        - HTTP 429: rate limited; log WARNING and return empty set.
        - Response URL contains consent.google.com: consent wall hit; log
          WARNING and return empty set (not parsed as results).
        - Response body contains "unusual traffic": bot detection; log WARNING
          and return empty set.

        URL extraction uses BeautifulSoup on ``div.yuRUbf a`` selector first
        (RESEARCH Pattern 5 -- LOW confidence, may break on Google HTML changes).
        Falls back to raw regex over the full response body when selector yields
        nothing (Assumption A2 degradation path).

        Per SCRP-17, D-13, D-14, Pitfall 6, RESEARCH Pattern 5, Assumption A2.

        Args:
            client: Shared AsyncClient.
            sem: Semaphore(1) bounding concurrent requests to Google.
            ext: Archive extension to query (e.g., "tar.gz").

        Returns:
            Set of GPL archive URLs from this extension's SERP page, or empty set
            on any block or transport error.

        """
        params = {"q": f"site:static.tp-link.com {ext}", "num": "10"}
        headers = {"User-Agent": _GOOGLE_UA, "Accept-Language": "en-US,en;q=0.9"}
        try:
            async with sem:
                r = await client.get(_GOOGLE_SEARCH, params=params, headers=headers, follow_redirects=True)
        except (httpx.TimeoutException, httpx.RequestError) as err:
            _log.debug("Google SERP request for %s failed: %s", ext, err)
            return set()

        # Block detection (Pitfall 6) -- each signal is logged and returns immediately
        if r.status_code == _HTTP_TOO_MANY_REQUESTS:
            _log.warning("Google blocked (429) on extension %s; skipping", ext)
            return set()
        if "consent.google.com" in str(r.url):
            _log.warning("Google consent wall hit on extension %s; skipping", ext)
            return set()
        if "unusual traffic" in r.text.lower():
            _log.warning("Google unusual-traffic block on extension %s; skipping", ext)
            return set()

        # Primary selector (LOW confidence per RESEARCH A2 -- may break on Google HTML changes).
        # Finds <a> tags inside div.yuRUbf containers whose href contains a known GPL CDN hostname.
        soup = BeautifulSoup(r.text, "lxml")
        urls: set[str] = set()
        for tag in soup.select("div.yuRUbf a"):
            href = tag.get("href", "")
            if isinstance(href, str) and ("static.tp-link.com" in href or "static.mercusys.com" in href):
                urls.add(href)

        # Fallback: regex over full HTML when selector yields nothing (Assumption A2 degradation).
        # Strip trailing punctuation to avoid phantom URLs from surrounding HTML text (WR-01).
        if not urls:
            urls = {u.rstrip(_TRAILING_PUNCT) for u in _GPL_URL_RE.findall(r.text)}

        return urls
