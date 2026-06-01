"""Mercusys regional GPL page crawler — two-pass adapted per D-16, SCRP-13.

KEY DIFFERENCE from TP-Link's RegionalSource:
- Mercusys has NO productTree JavaScript variable on its GPL index pages.
- Mercusys has NO phppage/gpl-res-list.html sub-page endpoint.
- Pass 1 extracts model names from href="?model=X" anchor links on the index page.
- Pass 2 fetches each /{region}/support/gpl-code/?model=X page directly and
  extracts static.mercusys.com/gpl/ download hrefs via regex.
- All requests need follow_redirects=True because Mercusys uses a 302
  HTTPS→HTTP→HTTPS redirect chain (Pitfall 2, verified by live fetch RESEARCH §SCRP-13).

Per SCRP-13, D-16, D-17, D-18, FOUN-03.
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

_MERCUSYS_BASE = "https://www.mercusys.com"
_GPL_PAGE_TEMPLATE = "{base}/{region}/support/gpl-code/"
_HTTP_OK = 200

# 47 region codes from live location picker fetch (RESEARCH §SCRP-13).
# "en" is listed first: Mercusys states "GPL code is generic and we only provide
# English versions for global users" — /en/ is the authoritative index.
# Additional regions are scraped as a bounded sample to catch any region-specific
# models (Pitfall 5: scraping all 47 x ~50 models = thousands of redundant requests).
MERCUSYS_REGIONS: tuple[str, ...] = (
    "en",  # global English -- primary, covers all models per site notice
    "ca",
    "fr-ca",
    "mx",
    "ar",
    "br",
    "cl",
    "co",
    "pe",
    "ec",
    "cac",
    "au",
    "in",
    "my",
    "vn",
    "kz",
    "kr",
    "th",
    "bd",
    "hk",
    "id",
    "ph",
    "jp",
    "sg",
    "de",
    "es",
    "uk",
    "ua",
    "pt",
    "fr",
    "ro",
    "nordic",
    "bg",
    "cz",
    "it",
    "pl",
    "tr",
    "baltic",
    "hu",
    "gr",
    "eg",
    "pk",
    "za",
    "ae",
    "saudi",
    "ma",
)

# Regex for Mercusys GPL archive links — flat directory, no date hierarchy.
# Anchored to static.mercusys.com/gpl/ to exclude TP-Link or other domains.
# ) is excluded to match the sibling patterns in _github.py, _reddit.py, etc. (WR-02).
# Note: Mercusys filenames contain literal parens (e.g. A10(JP)V1_GPL.tar.bz2) but
# those internal parens open before the character class stops at ) — only a trailing
# ) from a markdown link or paren-wrapped context is wrongly absorbed without the exclusion.
_MERCUSYS_URL_RE: re.Pattern[str] = re.compile(r"https://static\.mercusys\.com/gpl/[^\s\"'<>)]+")

# Trailing punctuation stripped after extraction to avoid phantom URLs (WR-01/WR-02).
_TRAILING_PUNCT: str = ".,;:!?'\""

# Representative regional sample to check for region-specific models (Pitfall 5).
# /en/ is the primary scrape; these 3 regions cover major geographic zones.
# "us" is intentionally absent: the Mercusys US storefront uses the bare / path,
# not /us/, so "us" is not in MERCUSYS_REGIONS and would be silently dropped by
# the membership filter. Removing it makes config match reality (WR-03).
_SAMPLE_REGIONS: tuple[str, ...] = ("de", "jp", "br")


# --- Pure extraction helpers ---


def _extract_model_names(html: str) -> list[str]:
    """Extract model names from href="?model=X" anchors on a Mercusys GPL index page.

    Mercusys index pages list product models as anchor links with href="?model=ModelName".
    There is no productTree JS variable and no phppage endpoint — this is the only
    way to get the model list (verified by live fetch, RESEARCH §SCRP-13).

    Args:
        html: Raw HTML of the GPL index page.

    Returns:
        List of model name strings (stripped of the "?model=" prefix), in page order.
        Empty model strings (href="?model=") are excluded.

    """
    soup = BeautifulSoup(html, "lxml")
    models: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = str(tag["href"])
        if href.startswith("?model="):
            model = href[len("?model=") :]
            if model:
                models.append(model)
    return models


def _update_progress(progress: Progress | None, task_id: TaskID | None) -> None:
    """Advance a Rich progress bar by 1 if both progress and task_id are provided."""
    if progress is not None and task_id is not None:
        progress.update(task_id, advance=1)


# --- MercusysRegionalSource ---


class MercusysRegionalSource:
    """Two-pass Mercusys regional GPL page crawler (SCRP-13, D-16).

    Adapted from the TP-Link RegionalSource two-pass pattern but uses
    Mercusys-specific extraction (no productTree, no phppage):

    Pass 1: Fetch /en/ index page (+ 3-region sample: de, jp, br) to collect model names
            from href="?model=X" anchor links. /en/ is the authoritative list per
            Mercusys's own note: "GPL code is generic and we only provide English
            versions for global users." (Pitfall 5: avoid 47x50 redundant requests.)

    Pass 2: Fetch /{region}/support/gpl-code/?model={name} for each model and
            extract static.mercusys.com/gpl/ URLs via regex.

    All requests pass follow_redirects=True because Mercusys uses a 302
    HTTPS→HTTP→HTTPS redirect chain (Pitfall 2, RESEARCH §SCRP-13).

    Conforms to the ScrapeSource Protocol (D-17). Name used as output filename
    stem by ScrapeRunner (D-18).
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'mercusys_regional.txt'."""
        return "mercusys_regional"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,
    ) -> set[str]:
        """Execute the two-pass Mercusys crawler and return discovered GPL URLs.

        Pass 1 fetches index pages to build the model list; Pass 2 fetches each
        model page and extracts download URLs.

        Args:
            client: Shared AsyncClient (HTTP/2 connection reuse).
            progress: Rich Progress instance for live display, or None.
            task_pass1: Rich task ID for Pass 1 progress bar, or None.
            task_pass2: Rich task ID for Pass 2 progress bar, or None.

        Returns:
            Set of static.mercusys.com/gpl/ archive URLs (raw, not normalized).

        """
        # Semaphore(10): Mercusys is a single CDN with no observed rate limiting.
        # Higher than the "external API" default (3) but below TP-Link regional (50).
        # Per FOUN-03 and PATTERNS.md §mercusys.py constants note.
        sem = asyncio.Semaphore(10)

        # Regions to scrape in pass 1: /en/ (authoritative) + small sample (Pitfall 5).
        # Filter sample regions that are also in MERCUSYS_REGIONS for safety.
        sample = [r for r in _SAMPLE_REGIONS if r in MERCUSYS_REGIONS]
        # /en/ is always first; deduplicate in case it appears in sample
        scrape_regions = list(dict.fromkeys(["en", *sample]))

        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, total=len(scrape_regions))

        # Pass 1: fetch index pages and union model names
        model_names = await self._run_pass1(
            regions=scrape_regions,
            client=client,
            sem=sem,
            progress=progress,
            task_id=task_pass1,
        )
        _log.info("Mercusys Pass 1: %d unique model names from %d regions", len(model_names), len(scrape_regions))

        if progress is not None and task_pass2 is not None:
            progress.update(task_pass2, total=len(model_names), visible=True)

        # Pass 2: fetch model pages for /en/ (all models available in English per site note).
        # Region is always "en" — Mercusys states GPL is generic/English-only (Pitfall 5).
        urls = await self._run_pass2(
            model_names=model_names,
            client=client,
            sem=sem,
            progress=progress,
            task_id=task_pass2,
        )
        _log.info("Mercusys Pass 2: %d GPL URLs from %d model pages", len(urls), len(model_names))
        return urls

    async def _run_pass1(
        self,
        *,
        regions: list[str],
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        progress: Progress | None,
        task_id: TaskID | None,
    ) -> list[str]:
        """Pass 1: fetch index pages for each region and union model names.

        Fetches each region's GPL index page concurrently (bounded by sem).
        Returns deduplicated model names in stable order. Progress advances
        one tick per region fetched.

        Args:
            regions: Region codes to fetch (e.g., ["en", "de", "jp"]).
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.
            progress: Rich Progress instance, or None.
            task_id: Rich task ID for this pass, or None.

        Returns:
            Deduplicated list of model name strings.

        """
        all_models: list[str] = []

        async def _scrape_one_region(region: str) -> None:
            """Fetch one region index page and append model names. Never raises."""
            html = await self._fetch_region_index(region=region, client=client, sem=sem)
            if html is not None:
                models = _extract_model_names(html)
                all_models.extend(models)
                if models:
                    _log.debug("Mercusys region %s: %d models", region, len(models))
                else:
                    _log.debug("Mercusys region %s: no models found", region)
            else:
                _log.warning("Mercusys region %s: index page fetch failed", region)
            _update_progress(progress, task_id)

        async with asyncio.TaskGroup() as tg:
            for region in regions:
                tg.create_task(_scrape_one_region(region))

        # Deduplicate while preserving order (first occurrence wins)
        seen: set[str] = set()
        unique: list[str] = []
        for m in all_models:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique

    async def _run_pass2(
        self,
        *,
        model_names: list[str],
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        progress: Progress | None,
        task_id: TaskID | None,
    ) -> set[str]:
        """Pass 2: fetch per-model pages and extract static.mercusys.com/gpl/ URLs.

        Fetches /en/support/gpl-code/?model={name} for each model concurrently.
        Always uses "en" region: Mercusys states GPL is generic/English-only (Pitfall 5).
        Non-200 responses are skipped silently (per SCRP-13 isolation requirement).
        Progress advances one tick per model fetched.

        Args:
            model_names: Model name strings from pass 1.
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.
            progress: Rich Progress instance, or None.
            task_id: Rich task ID for this pass, or None.

        Returns:
            Set of static.mercusys.com/gpl/ URL strings.

        """
        all_urls: set[str] = set()
        # Always target /en/ for model pages: Mercusys GPL is language-agnostic (Pitfall 5).
        _region = "en"

        async def _fetch_one_model(model: str) -> None:
            """Fetch one model page and collect GPL URLs. Never raises."""
            urls = await self._fetch_model_page(client=client, sem=sem, region=_region, model=model)
            all_urls.update(urls)
            _update_progress(progress, task_id)

        async with asyncio.TaskGroup() as tg:
            for model in model_names:
                tg.create_task(_fetch_one_model(model))

        return all_urls

    @staticmethod
    async def _fetch_region_index(
        *,
        region: str,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> str | None:
        """Fetch a Mercusys GPL index page for one region with one retry.

        Uses follow_redirects=True because Mercusys uses a 302 HTTPS→HTTP→HTTPS
        redirect chain (Pitfall 2, RESEARCH §SCRP-13). Returns page HTML on success,
        None after exhausting retries. All httpx errors are caught internally.

        Args:
            region: Region code (e.g., "en", "de").
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.

        Returns:
            HTML body string on HTTP 200, or None on failure.

        """
        url = _GPL_PAGE_TEMPLATE.format(base=_MERCUSYS_BASE, region=region)

        for attempt in range(2):
            try:
                async with sem:
                    # follow_redirects=True: Mercusys 302 chain (Pitfall 2)
                    r = await client.get(url, follow_redirects=True)
            except httpx.TimeoutException:
                _log.warning("Timeout fetching Mercusys region %s (attempt %d)", region, attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                continue
            except httpx.RequestError as err:
                _log.warning("Network error fetching Mercusys region %s (attempt %d): %s", region, attempt + 1, err)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                continue

            if r.status_code != _HTTP_OK:
                _log.warning("Mercusys region %s returned HTTP %s (attempt %d)", region, r.status_code, attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                continue

            return r.text

        return None

    @staticmethod
    async def _fetch_model_page(
        *,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        region: str,
        model: str,
    ) -> list[str]:
        """Fetch one model page and extract static.mercusys.com/gpl/ URLs.

        Per SCRP-13: non-200 responses return empty list without aborting the run.
        Exceptions (TimeoutException, RequestError) are caught and logged at DEBUG
        level — these are expected for some models and should not warn excessively.

        Uses follow_redirects=True: Mercusys has a 302 HTTPS→HTTP→HTTPS chain
        on per-model pages too (Pitfall 2, RESEARCH §SCRP-13).

        Args:
            client: Shared AsyncClient.
            sem: Semaphore bounding concurrent requests.
            region: Region code for the URL (e.g., "en").
            model: Model name string (e.g., "MR60X", "Halo S12").

        Returns:
            List of static.mercusys.com/gpl/ URL strings, or empty list on failure.

        """
        url = f"{_MERCUSYS_BASE}/{region}/support/gpl-code/?model={model}"
        try:
            async with sem:
                # follow_redirects=True: Mercusys 302 chain (Pitfall 2)
                r = await client.get(url, follow_redirects=True)
            if r.status_code != _HTTP_OK:
                _log.debug("Mercusys model page %s/%s returned HTTP %s", region, model, r.status_code)
                return []
            # Strip trailing punctuation to avoid phantom URLs from surrounding markup (WR-01/WR-02).
            return [u.rstrip(_TRAILING_PUNCT) for u in _MERCUSYS_URL_RE.findall(r.text)]
        except (httpx.TimeoutException, httpx.RequestError) as err:
            _log.debug("Mercusys model page %s/%s failed: %s", region, model, err)
            return []
