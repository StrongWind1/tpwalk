"""Two-pass TP-Link regional GPL page crawler and productTree extractor.

Pass 1: Fetch all regional GPL pages concurrently (Semaphore(50)), extract
productTree JSON via regex + brace-walk, split items into direct URLs and
phppage (model, region) pairs. Falls back to HTML link extraction when
productTree is absent or an empty list (regions like 'be' and 'nz').

Pass 2: Fetch all phppage sub-pages concurrently (Semaphore(30)), extract
GPL archive URLs from HTML fragments via regex.

Per D-01 through D-10, SCRP-02, SCRP-03, FOUN-03.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import string
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# --- Region constants ---

# 37 known region codes confirmed live on 2026-05-28.
# These serve as the fallback guarantee (D-02): even if the location picker
# and brute-force both fail, these 37 regions are always scraped.
HARDCODED_REGIONS: frozenset[str] = frozenset(
    {
        "ar",
        "at",
        "au",
        "be",
        "br",
        "ch",
        "cl",
        "co",
        "cz",
        "de",
        "dk",
        "es",
        "fi",
        "fr",
        "gr",
        "hk",
        "hu",
        "id",
        "in",
        "it",
        "jp",
        "kr",
        "mx",
        "my",
        "nl",
        "no",
        "nz",
        "ph",
        "pl",
        "pt",
        "ro",
        "se",
        "sg",
        "th",
        "tw",
        "uk",
        "us",
        "vn",
    }
)

LOCATION_PICKER_URL: str = "https://www.tp-link.com/en/choose-your-location/"
GPL_PAGE_TEMPLATE: str = "https://www.tp-link.com/{region}/support/gpl-code/"

# --- Internal constants ---

# HTTP 200 is the only success status -- matches _head.py pattern
_HTTP_OK = 200

_PHPPAGE_URL: str = "https://www.tp-link.com/phppage/gpl-res-list.html"

# Match the full value of an href attribute pointing at the GPL bucket — capturing
# everything up to the closing quote, not the first whitespace. TP-Link GPL
# filenames frequently contain spaces, e.g. "OC300 1.20.7z" and
# "TL-MR110(EU) 3.20_gpl_src.tar.gz"; a bare-URL scan that stops at \s truncates
# them into non-existent (403) keys. See docs/GPL-RECON.md (filenames contain
# spaces, parentheses, and ampersands).
# The \s* after the opening quote tolerates leading whitespace inside the attribute
# value: TP-Link pads some hrefs (e.g. href=" https://.../11.4.tar"), which an
# anchored pattern would silently drop. Internal spaces in filenames are preserved
# by [^"']+; callers strip the captured value to shed any trailing pad.
_GPL_HREF_RE: re.Pattern[str] = re.compile(r"""href\s*=\s*["']\s*(https://static\.tp-link\.com/[^"']+)["']""")

# Archive extensions used to gate the HTML fallback to GPL tarballs. ".7z" was
# added after observing "OC300 1.20.7z"; extensions cannot be trusted for actual
# format (docs/GPL-RECON.md), but the suffix set still filters obvious non-archives.
_GPL_EXTENSIONS: tuple[str, ...] = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar", ".zip", ".rar", ".tgz", ".gz", ".bz2", ".xz", ".7z")


# --- Pure extraction functions ---


def _extract_product_tree(html: str) -> dict[str, list[dict]] | None:  # type: ignore[type-arg]
    """Extract productTree embedded JSON from TP-Link regional GPL page HTML.

    productTree is assigned as a JavaScript variable in a <script> tag:
      var productTree = {JSON_OBJECT};     (most regions)
      var productTree = [];                 (sparse regions like be, nz)

    Uses regex to locate the marker, then brace-walk to extract the JSON
    substring. This is O(n) and handles minified 400KB pages reliably --
    a DOTALL regex would be fragile.

    Returns the parsed dict if productTree is a populated dict, or None if
    productTree is missing, an empty list, or fails to parse.

    Per SCRP-02, D-05, RESEARCH.md Pattern 2.
    """
    m = re.search(r"var productTree\s*=\s*", html)
    if not m:
        return None

    raw = html[m.end() :]
    first = raw[0] if raw else ""

    if first == "[":
        # Empty-list form -- sparse region with no dynamic entries (Pitfall 1)
        _log.debug("productTree is empty list -- sparse region")
        return None

    if first != "{":
        _log.warning("productTree has unexpected first char: %r", first)
        return None

    # Brace-walk: track depth to find the matching closing brace
    depth = 0
    for i, ch in enumerate(raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[: i + 1])  # type: ignore[no-any-return]
                except json.JSONDecodeError as e:
                    _log.warning("productTree JSON parse failed: %s", e)
                    return None

    # Unterminated -- should never happen on well-formed pages
    return None


def _classify_tree_items(
    tree: dict[str, list[dict]],  # type: ignore[type-arg]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Split productTree items into direct URLs and phppage (model, region) pairs.

    Per D-07: the href field is either:
    - 'https://static.tp-link.com/...' -- direct GPL archive URL
    - '?model=ModelName'               -- phppage sub-page follow needed

    Only collects URLs matching https://static.tp-link.com/ (domain allowlist
    per T-02-02). Other href values are silently skipped.

    Args:
        tree: Parsed productTree dict keyed by category ID.

    Returns:
        direct_urls: list of direct GPL archive HTTPS URLs
        phppage_pairs: list of (model_name, region_code) for phppage follow

    """
    direct_urls: list[str] = []
    phppage_pairs: list[tuple[str, str]] = []

    for items in tree.values():
        for item in items:
            # TP-Link's productTree hrefs sometimes carry surrounding whitespace
            # (e.g. " https://.../11.4.tar" for Deco XE200). The anchored
            # startswith() then silently drops them. strip() trims only the ends --
            # internal spaces in real filenames ("OC300 1.20.7z") are preserved.
            href = item.get("href", "").strip()
            region = item.get("app_folder", "")
            if href.startswith("https://static.tp-link.com/"):
                direct_urls.append(href)
            elif href.startswith("?model="):
                model = href[len("?model=") :]
                phppage_pairs.append((model, region))

    return direct_urls, phppage_pairs


def _extract_phppage_urls(fragment: str) -> list[str]:
    """Extract GPL archive URLs from phppage HTML fragment.

    The fragment contains a <table class="list"> with <a href=...> links.
    URLs are read from the href attribute value (up to the closing quote), so
    filenames containing spaces are captured whole rather than truncated at the
    first space (the OC300 / TL-MR110(EU) failure mode). Empty string fragment
    means no GPL source for this model/region pair -- this is normal, not an error.

    Per SCRP-03, RESEARCH.md Pattern 3.

    Args:
        fragment: Raw HTML body from phppage response.

    Returns:
        List of static.tp-link.com URLs found, or empty list.

    """
    if not fragment.strip():
        return []
    # strip() sheds any trailing pad the greedy [^"']+ captured; internal filename
    # spaces survive (only the ends are trimmed).
    return [url.strip() for url in _GPL_HREF_RE.findall(fragment)]


def _extract_html_fallback_urls(html: str) -> list[str]:
    """Extract GPL archive URLs directly from HTML when productTree is absent or empty.

    Regions like 'be' and 'nz' have empty productTree but embed GPL links
    directly in the page HTML (e.g., for the ACS Server product).

    Only collects URLs ending in known archive extensions to avoid collecting
    image, CSS, or JS URLs from the same domain. Domain allowlist via regex
    pattern (T-02-03).

    Per D-06, RESEARCH.md HTML fallback.

    Args:
        html: Full page HTML string.

    Returns:
        List of static.tp-link.com archive URLs found, or empty list.

    """
    # The negated class intentionally excludes only quotes and '>' -- not
    # whitespace -- so archive filenames containing spaces survive intact. The
    # \s* after the opening quote tolerates leading pad (href=" https://..."); each
    # captured value is stripped so trailing pad does not defeat the extension gate.
    raw_links = (url.strip() for url in re.findall(r'href=["\']\s*(https://static\.tp-link\.com/[^"\'>]+)["\']', html))
    return [url for url in raw_links if any(url.lower().endswith(ext) for ext in _GPL_EXTENSIONS)]


# --- Region discovery functions ---

# Regex to extract 2-3 letter lowercase region codes from picker href patterns.
# Matches patterns like href="/us/", href="//www.tp-link.com/de/", href="/cac/"
_PICKER_REGION_RE: re.Pattern[str] = re.compile(r'href="[^"]*?/([a-z]{2,3})/"')


def _parse_location_picker(html: str) -> set[str]:
    """Parse the TP-Link location picker page to extract region codes.

    The picker page contains anchor tags with href patterns like /us/, /de/,
    /cac/ (Canada). Extracts all 2-3 letter lowercase codes from these hrefs.

    Per D-01 source 2, D-02.

    Args:
        html: Raw HTML of the location picker page.

    Returns:
        Set of region code strings found, or empty set.

    """
    return set(_PICKER_REGION_RE.findall(html))


# Brute-force scans every lowercase code of length _MIN_CODE_LEN.._MAX_CODE_LEN.
# Scope is the 676 two-letter codes (aa-zz) per D-01: that is the ISO-style
# region-code space TP-Link actually uses (us, de, jp, ...). The sweep is bounded
# to two letters deliberately -- a wider sweep is both unnecessary (the location
# picker is authoritative for longer codes such as 'cac') and actively harmful:
# the origin soft-404s every code to HTTP 200 (see _bruteforce_regions), so a
# wider sweep multiplies page volume past the origin's HTTP/2 max-streams limit
# and triggers server GOAWAY (ConnectionTerminated) storms mid-run.
_MIN_CODE_LEN: int = 2
_MAX_CODE_LEN: int = 2


def _generate_short_codes() -> frozenset[str]:
    """Generate every lowercase two-letter code ('aa' through 'zz').

    676 codes total (26 x 26). Used as the input set for brute-force region
    discovery (D-01 source 1). Longer real codes (e.g. 'cac' for Canada) are
    recovered from the location picker, not this sweep -- keeping the sweep at
    two letters holds total request volume well under the origin's HTTP/2
    max-streams-per-connection limit (observed GOAWAY at ~20k streams).
    """
    return frozenset("".join(combo) for length in range(_MIN_CODE_LEN, _MAX_CODE_LEN + 1) for combo in itertools.product(string.ascii_lowercase, repeat=length))


async def _bruteforce_regions(
    *,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> set[str]:
    """GET every aa-zz code and keep the ones whose page is a real GPL page.

    Per D-01 source 1, a code is a live region only if it returns a *valid GPL
    page*. The original implementation approximated "valid GPL page" with HTTP
    200, but that test is useless against the live origin:

        OBSERVED DEVIATION (verified 2026-05-31): www.tp-link.com returns HTTP
        200 for EVERY region code, real or not. A bogus code (e.g. 'zz', 'cla')
        serves an ~11 KB soft-404 stub whose only productTree is the empty form
        ``var productTree = []``; a real region serves a ~500 KB page with a
        populated ``var productTree = {...}``. So a status-only gate classifies
        all 676 codes as live, floods Pass 1 with hundreds of empty pages, and
        -- at the former 18k-code sweep -- pushed the shared HTTP/2 connection
        past the origin's max-streams limit, triggering GOAWAY
        (ConnectionTerminated) storms.

    The fix is a content gate: GET each candidate (HEAD cannot see the body) and
    accept it only when ``_extract_product_tree`` finds a populated productTree
    -- the same deterministic signal Pass 1 uses, never a heuristic. Redirects
    are followed so locale aliases (e.g. 'at' -> /de/, 'dk' -> /nordic/) validate
    correctly. Bounded by Semaphore(50); 676 GETs complete in seconds and stay
    far below the GOAWAY threshold.

    Args:
        client: Shared AsyncClient.
        sem: Semaphore bounding concurrent requests.

    Returns:
        Set of region codes whose GPL page contains a populated productTree.

    """
    all_codes = _generate_short_codes()
    live: set[str] = set()

    async def _check_code(code: str) -> None:
        """GET one region code; keep it only if its page is a real GPL page."""
        async with sem:
            try:
                r = await client.get(
                    GPL_PAGE_TEMPLATE.format(region=code),
                    follow_redirects=True,
                )
            except httpx.RequestError:
                return
        # Content gate, not status gate: the origin soft-404s every code to 200,
        # so a populated productTree is the only reliable liveness signal.
        if r.status_code == _HTTP_OK and _extract_product_tree(r.text) is not None:
            live.add(code)

    async with asyncio.TaskGroup() as tg:
        for code in all_codes:
            tg.create_task(_check_code(code))

    return live


async def discover_regions(
    *,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> frozenset[str]:
    """Discover all live TP-Link GPL regions from three sources.

    Three-source discovery per D-01:
    1. Brute-force every length 0-3 code (18,279, '' through zzz) via HEAD checks
    2. Fetch and parse the location picker page for dynamic codes
    3. HARDCODED_REGIONS as fallback guarantee

    The union of all three ensures maximum coverage. Picker failures are
    non-fatal (D-02) -- brute-force + hardcoded still cover everything.

    Args:
        client: Shared AsyncClient.
        sem: Semaphore bounding concurrent requests.

    Returns:
        Frozenset of all discovered region codes.

    """
    # Source 1: brute-force all length 0-3 codes
    brute_results = await _bruteforce_regions(client=client, sem=sem)
    _log.info("Brute-force discovered %d live region codes", len(brute_results))

    # Source 2: location picker page
    picker_results: set[str] = set()
    try:
        r = await client.get(LOCATION_PICKER_URL, follow_redirects=False)
        if r.status_code == _HTTP_OK:
            picker_results = _parse_location_picker(r.text)
            _log.info("Picker discovered %d region codes", len(picker_results))
        else:
            _log.warning("Location picker returned HTTP %s -- using hardcoded fallback", r.status_code)
    except httpx.RequestError as err:
        _log.warning("Location picker fetch failed: %s -- using hardcoded fallback", err)

    # Source 3: hardcoded fallback (D-02 guarantee)
    all_regions = brute_results | picker_results | HARDCODED_REGIONS
    _log.info("Total unique regions: %d (brute=%d, picker=%d, hardcoded=%d)", len(all_regions), len(brute_results), len(picker_results), len(HARDCODED_REGIONS))

    return frozenset(all_regions)


# --- RegionalSource: two-pass crawler ---


class RegionalSource:
    """Two-pass TP-Link regional GPL page crawler.

    Pass 1: Fetch all regional GPL pages concurrently (Semaphore(50)), extract
    productTree JSON, classify items into direct URLs and phppage pairs. Falls
    back to HTML link extraction when productTree is absent (D-06).

    Pass 2: Fetch all phppage sub-pages concurrently (Semaphore(30)), extract
    GPL archive URLs from HTML fragments.

    Error handling per D-03: region errors get ONE retry with 1-second delay,
    then skip. A single bad region never aborts the run (Pitfall 2). phppage
    errors skip silently (high-volume, no retry needed).

    Conforms to the ScrapeSource Protocol (D-11). The name property is used as
    the output filename stem by ScrapeRunner (D-14).

    Per D-01 through D-10, SCRP-01 through SCRP-05.
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'regional_crawl.txt'."""
        return "regional_crawl"

    def __init__(self) -> None:
        """Initialize pass counters for ScrapeStats reporting.

        These are public attributes read by ScrapeRunner after run() completes
        to populate ScrapeStats per D-15.
        """
        self.pass1_count: int = 0
        self.pass2_count: int = 0
        self.regions_scraped: int = 0
        self.regions_failed: int = 0

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,
    ) -> set[str]:
        """Execute the two-pass regional crawler and return discovered URLs.

        Pass 1 discovers regions, fetches their GPL pages, and extracts
        productTree items. Pass 2 follows phppage sub-pages for items that
        need model-specific resolution.

        Args:
            client: Shared AsyncClient (HTTP/2 connection reuse).
            progress: Rich Progress instance for live display, or None.
            task_pass1: Rich task ID for Pass 1 progress bar, or None.
            task_pass2: Rich task ID for Pass 2 progress bar, or None.

        Returns:
            Set of all discovered GPL archive URLs (raw, not normalized).

        """
        sem_pages = asyncio.Semaphore(50)  # D-04
        sem_phppage = asyncio.Semaphore(30)  # D-09

        # 1. Discover regions (D-01)
        regions = await discover_regions(client=client, sem=sem_pages)
        _log.info("Starting Pass 1 with %d regions", len(regions))

        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, total=len(regions))

        # 2. Pass 1
        direct_urls, phppage_pairs = await self._run_pass1(
            regions=regions,
            client=client,
            sem=sem_pages,
            progress=progress,
            task_id=task_pass1,
        )
        _log.info("Pass 1 complete: %d direct URLs, %d phppage pairs from %d regions (%d failed)", len(direct_urls), len(phppage_pairs), self.regions_scraped, self.regions_failed)

        # 3. Pass 2
        phppage_urls = await self._run_pass2(
            phppage_pairs=phppage_pairs,
            client=client,
            sem=sem_phppage,
            progress=progress,
            task_id=task_pass2,
        )
        _log.info("Pass 2 complete: %d phppage URLs", len(phppage_urls))

        return set(direct_urls) | set(phppage_urls)

    async def _run_pass1(
        self,
        *,
        regions: frozenset[str],
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        progress: Progress | None,
        task_id: TaskID | None,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Pass 1: fetch regional GPL pages and extract productTree items.

        Returns (direct_urls, phppage_pairs). Updates instance counters for
        ScrapeStats reporting.
        """
        direct_urls: list[str] = []
        phppage_pairs: list[tuple[str, str]] = []
        regions_scraped = 0
        regions_failed = 0

        async def _scrape_one(region: str) -> None:
            """Fetch one region page and classify items. Never raises."""
            nonlocal regions_scraped, regions_failed

            response_text = await self._fetch_region_with_retry(region, client=client, sem=sem)

            if response_text is None:
                regions_failed += 1
                _update_progress(progress, task_id)
                return

            regions_scraped += 1
            self._classify_region_page(response_text, region, direct_urls, phppage_pairs)
            _update_progress(progress, task_id)

        async with asyncio.TaskGroup() as tg:
            for region in sorted(regions):
                tg.create_task(_scrape_one(region))

        self.pass1_count = len(direct_urls)
        self.regions_scraped = regions_scraped
        self.regions_failed = regions_failed
        return direct_urls, phppage_pairs

    async def _run_pass2(
        self,
        *,
        phppage_pairs: list[tuple[str, str]],
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        progress: Progress | None,
        task_id: TaskID | None,
    ) -> list[str]:
        """Pass 2: fetch phppage sub-pages and extract GPL URLs.

        Phppage errors are silently skipped (high-volume, no retry needed).
        Per D-08, D-09.
        """
        _log.info("Starting Pass 2 with %d phppage requests", len(phppage_pairs))

        if progress is not None and task_id is not None:
            progress.update(task_id, total=len(phppage_pairs), visible=True)

        phppage_urls: list[str] = []

        async def _fetch_one(model: str, region: str) -> None:
            """Fetch one phppage and extract URLs. Never raises."""
            async with sem:
                try:
                    r = await client.get(
                        _PHPPAGE_URL,
                        params={"model": model, "appPath": region},
                        headers={"Referer": f"https://www.tp-link.com/{region}/support/gpl-code/"},
                    )
                except httpx.TimeoutException, httpx.RequestError:
                    _update_progress(progress, task_id)
                    return

            phppage_urls.extend(_extract_phppage_urls(r.text))
            _update_progress(progress, task_id)

        async with asyncio.TaskGroup() as tg:
            for model, region in phppage_pairs:
                tg.create_task(_fetch_one(model, region))

        self.pass2_count = len(phppage_urls)
        return phppage_urls

    @staticmethod
    async def _fetch_region_with_retry(
        region: str,
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> str | None:
        """Fetch a regional GPL page with one retry on failure (D-03).

        Redirects are followed: several real regions are locale aliases that
        301/302 to an aggregated page ('at' -> /de/, 'dk' -> /nordic/, plus fi,
        il, kg, no, se, uz). With follow_redirects=False those returned a bare
        3xx, were treated as failures, and silently dropped real coverage. Some
        aggregated targets (e.g. /nordic/) are reachable only via the redirect,
        so following it adds regions the picker never lists.

        Returns the response body text on success, or None after exhausting
        retries. Catches all httpx errors internally.
        """
        gpl_url = GPL_PAGE_TEMPLATE.format(region=region)

        for attempt in range(2):
            try:
                async with sem:
                    r = await client.get(gpl_url, follow_redirects=True)
            except httpx.TimeoutException:
                _log.warning("Timeout fetching region %s (attempt %d)", region, attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                continue
            except httpx.RequestError as err:
                _log.warning("Network error fetching region %s (attempt %d): %s", region, attempt + 1, err)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                continue

            if r.status_code != _HTTP_OK:
                _log.warning("Region %s returned HTTP %s (attempt %d)", region, r.status_code, attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                continue

            return r.text

        return None

    @staticmethod
    def _classify_region_page(
        html: str,
        region: str,
        direct_urls: list[str],
        phppage_pairs: list[tuple[str, str]],
    ) -> None:
        """Extract productTree from a region page and classify items.

        Falls back to HTML link extraction when productTree is absent (D-06).
        Appends results to the provided lists in place.
        """
        tree = _extract_product_tree(html)
        if tree is not None:
            region_direct, region_phppage = _classify_tree_items(tree)
            direct_urls.extend(region_direct)
            phppage_pairs.extend(region_phppage)
        else:
            fallback = _extract_html_fallback_urls(html)
            direct_urls.extend(fallback)
            if fallback:
                _log.debug("Region %s: HTML fallback found %d URLs", region, len(fallback))


def _update_progress(progress: Progress | None, task_id: TaskID | None) -> None:
    """Advance a Rich progress bar by 1 if both progress and task_id are set."""
    if progress is not None and task_id is not None:
        progress.update(task_id, advance=1)
