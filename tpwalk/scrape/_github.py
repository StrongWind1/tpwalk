"""GitHub community source for the tpwalk scrape pipeline.

Implements two ScrapeSource-conforming classes:

- GitHubSearchSource (name='github_search'): Broad authenticated code + issue
  search across all of GitHub for `static.tp-link.com` GPL URL references
  (SCRP-08) and the Mercusys prefix (SCRP-09). Rate-limit aware (SCRP-10):
  respects Retry-After / X-RateLimit-Reset headers and backs off on secondary
  rate limits (which fire even when X-RateLimit-Remaining > 0, see Pitfall 1).

- TPLinkGitHubSource (name='tplink_github'): Scans the TP-LINK GitHub org
  (github.com/TP-LINK, currently 1 repo "Romesburg" last updated 2015 --
  verified 04-RESEARCH.md open research items) via user:TP-LINK qualified
  search and bounded wiki clone. Returns gracefully empty when the org is thin
  (SCRP-16, D-07, D-08).

Both sources:
- Read GITHUB_TOKEN from environment only (D-01). When unset, they skip with
  a WARNING and the run continues uninterrupted (D-02, SCRP-05).
- Never log the token value -- only its presence/absence (ASVS V7, T-04-01).
- Use asyncio.Semaphore(3) per FOUN-03 (external-API category, same as CDX).

Registration into ScrapeRunner._sources is handled by plan 04-06 (Wave 2).

Per SCRP-08, SCRP-09, SCRP-10, SCRP-16, D-01, D-02, D-04, D-05, D-07, D-08, FOUN-03.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# --- Module-level constants ---

_GITHUB_API: str = "https://api.github.com"
_HTTP_OK: int = 200

# GitHub hard-caps search results at 1,000 per query (10 pages x 100/page).
# Paginating beyond page 10 is useless and wastes API quota (Pitfall 3).
_MAX_GITHUB_PAGES: int = 10

# The GitHub-documented search result cap; queries returning more are truncated.
_GITHUB_RESULT_CAP: int = 1000

# The TP-LINK GitHub org slug (verified: github.com/TP-LINK, 1 repo "Romesburg",
# last updated 2015 -- org is thin; value of SCRP-16 is the user:TP-LINK search).
_TPLINK_ORG: str = "TP-LINK"

# Search queries covering both TP-Link GPL prefixes (SCRP-08) and the Mercusys
# prefix (SCRP-09). The `in:file` qualifier targets file content (code + READMEs).
_SEARCH_QUERIES: tuple[str, ...] = (
    "static.tp-link.com/upload/gpl-code in:file",  # current date-hierarchical path
    "static.tp-link.com/resources/gpl in:file",  # legacy flat path
    "static.mercusys.com/gpl in:file",  # Mercusys sub-brand (SCRP-09)
)

# Anchored regex matching GPL archive URLs on both CDN domains.
# Character class excludes whitespace and HTML-unsafe chars to avoid runaway matches.
_GPL_URL_RE: re.Pattern[str] = re.compile(r"https://static\.(?:tp-link|mercusys)\.com/[^\s\"'<>)]+")

# Grep pattern for the shell (POSIX ERE syntax -- note: must match _GPL_URL_RE intent).
_GPL_GREP_PATTERN: str = r"https://static\.(tp-link|mercusys)\.com/[^[:space:]\"'<>)]+"

# Trailing punctuation that must be stripped after regex extraction from free-text fields
# (e.g. prose sentences: "...file.tar.gz." or "...file.tar.gz,"). The regex character class
# stops at ) < > but sentence punctuation like . , ; : ! is absorbed into the match (WR-01).
_TRAILING_PUNCT: str = ".,;:!?'\""


# --- Module-level helpers ---


def _parse_retry_seconds(value: str | None) -> float | None:
    """Parse a Retry-After or X-RateLimit-Reset header value as a delay in seconds.

    RFC 9110 §10.2.3 allows Retry-After to be either delta-seconds (integer) or an
    HTTP-date (e.g. "Wed, 21 Oct 2025 07:28:00 GMT"). float() raises ValueError on
    the date form; we handle both and return None on any unparseable value so the
    caller can fall through to exponential backoff instead of crashing.

    For X-RateLimit-Reset the value is always a Unix epoch integer, so the float()
    path is sufficient, but we guard it the same way for symmetry.

    Args:
        value: Raw header value string, or None if the header was absent.

    Returns:
        Delay in seconds as a float if parseable, or None if absent/malformed.

    """
    if not value:
        return None
    # Fast path: numeric delta-seconds or Unix epoch
    try:
        return float(value)
    except ValueError:
        pass
    # Slow path: HTTP-date form (RFC 9110 §10.2.3), e.g. "Wed, 21 Oct 2025 07:28:00 GMT"
    try:
        dt = email.utils.parsedate_to_datetime(value)
        # Return remaining seconds until the given date (may be 0 if in the past)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:  # noqa: BLE001 -- malformed date is not actionable; fall through
        return None


# --- Request context dataclass ---


@dataclass(frozen=True)
class _GitHubCtx:
    """Bundle of the shared context needed for every GitHub API call.

    Reduces argument count for _fetch_github_with_retry and _search_endpoint
    from 6 to 3 keyword-only args (FOUN-03, PLR0913).
    """

    client: httpx.AsyncClient
    sem: asyncio.Semaphore
    headers: dict[str, str]


# --- Shared module-level helpers ---


async def _fetch_github_with_retry(
    *,
    ctx: _GitHubCtx,
    url: str,
    params: dict[str, str],
    max_retries: int = 3,
) -> httpx.Response | None:
    """Fetch a GitHub API URL with retry, exponential backoff, and rate-limit handling.

    Differs from the plain _fetch_with_retry in _wayback.py by also handling
    GitHub secondary rate limits (HTTP 403 or 429 WITH Retry-After header).
    Secondary limits fire even when X-RateLimit-Remaining > 0, so we must not
    rely on the remaining count alone (Pitfall 1, RESEARCH.md).

    Rate-limit priority:
      1. If response status in {403, 429} and Retry-After present: sleep that
         many seconds then continue (secondary rate limit per Pitfall 1).
      2. If X-RateLimit-Reset present: sleep until reset epoch + 1s.
      3. Otherwise: log warning and fall through to exponential backoff.

    Exponential backoff shape: 2s, 4s, 8s (same as _wayback.py per D-05).

    Per D-05, SCRP-10, T-04-05.

    Args:
        ctx: Shared GitHub request context (client, semaphore, auth headers).
        url: Full GitHub API endpoint URL.
        params: Query parameters.
        max_retries: Maximum retry attempts after first failure.

    Returns:
        httpx.Response on HTTP 200, or None after exhausting retries.

    """
    for attempt in range(max_retries + 1):
        try:
            async with ctx.sem:
                r = await ctx.client.get(url, params=params, headers=ctx.headers)
            if r.status_code == _HTTP_OK:
                return r
            # Secondary rate limit: 403 or 429, check Retry-After first (Pitfall 1).
            # _parse_retry_seconds handles both delta-seconds and HTTP-date forms
            # (RFC 9110 §10.2.3) and returns None on any malformed value so we
            # fall through to exponential backoff rather than crashing on ValueError.
            if r.status_code in {403, 429}:
                retry_secs = _parse_retry_seconds(r.headers.get("retry-after"))
                if retry_secs is not None:
                    _log.warning("GitHub secondary rate limit; sleeping %.1f s (Retry-After)", retry_secs)
                    await asyncio.sleep(retry_secs)
                    continue
                # X-RateLimit-Reset is always a Unix epoch integer from GitHub's API
                reset_raw = r.headers.get("x-ratelimit-reset")
                if reset_raw is not None:
                    try:
                        wait = max(0.0, float(reset_raw) - time.time()) + 1.0
                    except ValueError:
                        wait = 2.0 * (2**attempt)  # fall back to exponential backoff shape
                    _log.warning("GitHub rate limit; sleeping %.1f s until X-RateLimit-Reset", wait)
                    await asyncio.sleep(wait)
                    continue
            _log.warning("GitHub %s returned HTTP %s (attempt %d/%d)", url, r.status_code, attempt + 1, max_retries + 1)
        except httpx.TimeoutException:
            # Catch TimeoutException before RequestError -- it is a subclass.
            _log.warning("GitHub %s timed out (attempt %d/%d)", url, attempt + 1, max_retries + 1)
        except httpx.RequestError as err:
            _log.warning("GitHub %s failed: %s (attempt %d/%d)", url, err, attempt + 1, max_retries + 1)
        if attempt < max_retries:
            await asyncio.sleep(2.0 * (2**attempt))  # 2s, 4s, 8s per D-05
    return None


async def _search_endpoint(
    *,
    ctx: _GitHubCtx,
    endpoint: str,
    query: str,
) -> set[str]:
    """Paginate one GitHub search endpoint and extract GPL archive URLs from fragments.

    Uses `Accept: application/vnd.github.text-match+json` (set in ctx.headers) so
    the API returns text_matches[].fragment -- the matched snippet containing the URL.
    This avoids N+1 per-file fetches (see RESEARCH.md "Don't Hand-Roll").

    Pagination: stops at _MAX_GITHUB_PAGES (page 10 = 1,000 results, the GitHub
    hard cap). Also stops early when items is empty. Logs a WARNING when
    total_count > 1,000 (Pitfall 3).

    Per SCRP-08, SCRP-09, SCRP-10, D-04, D-06.

    Args:
        ctx: Shared GitHub request context.
        endpoint: Path segment, e.g. "/search/code" or "/search/issues".
        query: The `q` parameter value.

    Returns:
        Set of extracted GPL archive URL strings from all result fragments.

    """
    urls: set[str] = set()
    for page in range(1, _MAX_GITHUB_PAGES + 1):
        params: dict[str, str] = {"q": query, "per_page": "100", "page": str(page)}
        response = await _fetch_github_with_retry(
            ctx=ctx,
            url=f"{_GITHUB_API}{endpoint}",
            params=params,
        )
        if response is None:
            break
        data = response.json()
        items = data.get("items", [])
        if not items:
            break
        # Warn on 1,000-result truncation (Pitfall 3) -- only check on page 1
        if page == 1:
            total = data.get("total_count", 0)
            if total > _GITHUB_RESULT_CAP:
                _log.warning(
                    "GitHub query %r returned %d total; GitHub caps at 1,000. Consider splitting the query.",
                    query,
                    total,
                )
        # Extract URLs from text_matches fragments (avoids N+1 per-file fetches).
        # Strip trailing sentence punctuation (.,:;!?'") after regex extraction —
        # free-text fragments often end with a period that the regex absorbs (WR-01).
        for item in items:
            for match in item.get("text_matches", []):
                urls.update(u.rstrip(_TRAILING_PUNCT) for u in _GPL_URL_RE.findall(match.get("fragment", "")))
    return urls


# --- Source classes ---


class GitHubSearchSource:
    """Broad GitHub code + issue search for GPL archive URL references.

    Searches GitHub's authenticated REST API for both TP-Link GPL URL prefixes
    (SCRP-08) and the Mercusys prefix (SCRP-09). Rate-limit aware (SCRP-10).

    When GITHUB_TOKEN is absent, returns set() and logs WARNING -- never raises,
    never makes unauthenticated requests (D-01, D-02). The run continues via
    ScrapeRunner's exception isolation (SCRP-05).

    Output filename: 'github_search.txt' (D-04, D-18).
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'github_search.txt'."""
        return "github_search"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
    ) -> set[str]:
        """Execute GitHub code + issue search across all query prefixes.

        Credential guard: reads GITHUB_TOKEN from environment; returns set() with
        WARNING if unset. Never logs the token value (ASVS V7, T-04-01).

        For each query in _SEARCH_QUERIES, searches both /search/code and
        /search/issues and unions results. URL extraction via text_match fragments
        avoids N+1 per-file fetches.

        Per SCRP-08, SCRP-09, SCRP-10, D-04.

        Args:
            client: Shared AsyncClient.
            progress: Rich Progress instance for live display, or None.
            task_pass1: Rich task ID for prefix progress, or None.
            task_pass2: Unused by this source.

        Returns:
            Set of raw GPL archive URL strings (normalized by ScrapeRunner).

        """
        # Credential guard (D-01, D-02) -- must be first action in run()
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            _log.warning(
                "GITHUB_TOKEN is not set; skipping %s (D-02). Set GITHUB_TOKEN to enable GitHub search.",
                self.name,
            )
            return set()

        # Log presence only -- never log the token value (ASVS V7, T-04-01)
        _log.debug("GITHUB_TOKEN present: %s", bool(token))

        ctx = _GitHubCtx(
            client=client,
            sem=asyncio.Semaphore(3),  # FOUN-03: external-API category
            headers={
                "Authorization": f"Bearer {token}",
                # text-match returns fragment snippets; URLs visible without N+1 fetches
                "Accept": "application/vnd.github.text-match+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        if progress is not None and task_pass1 is not None:
            # Each query runs 2 endpoints; total steps = queries * 2
            progress.update(task_pass1, total=len(_SEARCH_QUERIES) * 2)

        all_urls: set[str] = set()
        for query in _SEARCH_QUERIES:
            for endpoint in ("/search/code", "/search/issues"):
                urls = await _search_endpoint(ctx=ctx, endpoint=endpoint, query=query)
                all_urls |= urls
                if progress is not None and task_pass1 is not None:
                    progress.update(task_pass1, advance=1)

        _log.info("GitHubSearchSource: %d URLs from %d queries x 2 endpoints", len(all_urls), len(_SEARCH_QUERIES))
        return all_urls


class TPLinkGitHubSource:
    """TP-LINK org scan + bounded wiki clone for GPL archive URL references.

    Scans the TP-LINK GitHub org (github.com/TP-LINK) via a user:TP-LINK
    qualified code/issue search and, for each repo with a wiki, clones and
    greps it for GPL URLs.

    The org is thin (1 repo "Romesburg", last updated 2015 -- verified in
    04-RESEARCH.md open research items), so this source completes quickly and
    is expected to return a mostly-empty set. The high-value GitHub work is the
    broad search in GitHubSearchSource.

    Separate output file ('tplink_github.txt') from 'github_search.txt' per
    Phase 4 success criterion #1 (D-07).

    Per SCRP-16, D-07, D-08, D-09.
    """

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'tplink_github.txt'."""
        return "tplink_github"

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- ScrapeSource Protocol requires this parameter
    ) -> set[str]:
        """Scan TP-LINK org repos and wiki clones for GPL archive URLs.

        Two sub-tasks (D-07, D-08):
        (a) user:TP-LINK qualified code/issue search via _search_endpoint.
        (b) Fetch org repo list, clone any wikis, grep for GPL URLs.

        Credential guard matches GitHubSearchSource. Never makes unauthenticated
        requests. Degrades gracefully when the org has no content or git is absent.

        Per SCRP-16, D-07, D-08.

        Args:
            client: Shared AsyncClient.
            progress: Rich Progress instance, or None.
            task_pass1: Rich task ID, or None.
            task_pass2: Unused.

        Returns:
            Set of raw GPL archive URL strings from org search + wiki grep.

        """
        # Credential guard (D-01, D-02) -- same pattern as GitHubSearchSource
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            _log.warning(
                "GITHUB_TOKEN is not set; skipping %s (D-02). Set GITHUB_TOKEN to enable TP-LINK org scan.",
                self.name,
            )
            return set()

        _log.debug("GITHUB_TOKEN present: %s", bool(token))

        ctx = _GitHubCtx(
            client=client,
            sem=asyncio.Semaphore(3),  # FOUN-03: external-API category
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.text-match+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        all_urls: set[str] = set()

        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, total=4)  # search*2 + repos + wikis

        # (a) user:TP-LINK qualified search (the practical SCRP-16 value per RESEARCH)
        for endpoint in ("/search/code", "/search/issues"):
            urls = await _search_endpoint(
                ctx=ctx,
                endpoint=endpoint,
                query=f"static.tp-link.com/upload/gpl-code user:{_TPLINK_ORG} in:file",
            )
            all_urls |= urls
            if progress is not None and task_pass1 is not None:
                progress.update(task_pass1, advance=1)

        # (b) Fetch org repo list via GitHub API
        repos = await self._fetch_org_repos(ctx=ctx)
        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, advance=1)

        # (b2) Clone and grep wikis for repos that have them (D-08)
        for repo in repos:
            html_url = str(repo.get("html_url") or "")
            has_wiki = bool(repo.get("has_wiki", False))
            if has_wiki and html_url:
                wiki_urls = await self._grep_wiki(repo_html_url=html_url)
                all_urls |= wiki_urls

        if progress is not None and task_pass1 is not None:
            progress.update(task_pass1, advance=1)

        _log.info("TPLinkGitHubSource: %d URLs from org search + %d repo wikis", len(all_urls), len(repos))
        return all_urls

    async def _fetch_org_repos(self, *, ctx: _GitHubCtx) -> list[dict[str, object]]:
        """Fetch the TP-LINK org repo list from GitHub API.

        Tries /orgs/{org}/repos first; falls back to /users/{org}/repos if the
        org endpoint returns a non-200 response. Both can return the Romesburg
        repo (verified live). Returns empty list on failure -- never raises.

        Uses the standard REST Accept header (application/vnd.github+json) rather
        than the search text-match header (application/vnd.github.text-match+json).
        The text-match header is documented only for /search/* endpoints and is
        meaningless — and potentially fragile — on the repos list endpoint (WR-04).
        The token is not logged (ASVS V7, T-04-01).

        Per D-09, SCRP-16.

        Args:
            ctx: Shared GitHub request context.

        Returns:
            List of repo dicts from GitHub API (may be empty).

        """
        # Override Accept to the standard REST media type for repo-list endpoints (WR-04).
        # The text-match header in ctx.headers is search-only; sending it to /orgs/*/repos
        # is benign today but fragile against future API stricter media-type handling.
        repo_headers = {**ctx.headers, "Accept": "application/vnd.github+json"}
        repo_ctx = _GitHubCtx(client=ctx.client, sem=ctx.sem, headers=repo_headers)
        for path in (f"/orgs/{_TPLINK_ORG}/repos", f"/users/{_TPLINK_ORG}/repos"):
            response = await _fetch_github_with_retry(
                ctx=repo_ctx,
                url=f"{_GITHUB_API}{path}",
                params={"per_page": "100"},
            )
            if response is not None and response.status_code == _HTTP_OK:
                data = response.json()
                if isinstance(data, list):
                    return data  # type: ignore[return-value]
        return []

    async def _grep_wiki(self, *, repo_html_url: str) -> set[str]:
        """Clone a GitHub repo's wiki and grep it for GPL archive URLs.

        Security (T-04-03, ASVS V5): asserts repo_html_url host is github.com
        before invoking subprocess to prevent a malformed API response from
        redirecting the clone to an arbitrary target.

        Runs the clone+grep inside run_in_executor so the subprocess does NOT
        block the asyncio event loop (RESEARCH anti-pattern warning).

        Degrades gracefully on every error path: missing git CLI, non-existent
        wiki, clone failure, grep failure -- all return set() (D-08).

        Per D-08, SCRP-16, T-04-03.

        Args:
            repo_html_url: GitHub repo HTML URL from the API response,
                e.g. "https://github.com/TP-LINK/Romesburg".

        Returns:
            Set of GPL archive URL strings found in the wiki, or empty set.

        """
        # Security gate: only clone from github.com (T-04-03, ASVS V5 -- Tampering)
        # repo_html_url is from the GitHub API, but a malformed response could still
        # inject an arbitrary target; host check is a correctness invariant.
        try:
            parsed = urllib.parse.urlparse(repo_html_url)
            if parsed.netloc != "github.com":
                _log.warning("_grep_wiki: rejecting non-github.com URL: %s", repo_html_url)
                return set()
        except Exception:  # noqa: BLE001
            _log.warning("_grep_wiki: failed to parse URL: %s", repo_html_url)
            return set()

        wiki_url = f"{repo_html_url}.wiki.git"
        loop = asyncio.get_running_loop()

        # Resolve absolute paths at call site (avoids S607: partial executable path).
        git_bin = shutil.which("git") or "git"
        grep_bin = shutil.which("grep") or "grep"

        def _clone_and_grep() -> set[str]:
            """Run git clone + grep in a thread pool (avoids blocking event loop).

            wiki_url is constructed from a validated github.com URL (host checked above),
            so the subprocess input is controlled (S603 -- execution of untrusted input).
            capture_output=True keeps any embedded credential out of stdout/shell history.
            """
            with tempfile.TemporaryDirectory() as tmpdir:
                clone_result = subprocess.run(  # noqa: S603 -- wiki_url validated to github.com above
                    [git_bin, "clone", "--depth=1", wiki_url, tmpdir],
                    check=False,  # non-zero = no wiki or git absent; return empty
                    capture_output=True,  # capture_output keeps credentials out of stdout
                )
                if clone_result.returncode != 0:
                    _log.debug("Wiki clone failed for %s (returncode=%d)", wiki_url, clone_result.returncode)
                    return set()
                # grep for GPL URL patterns in the cloned wiki directory
                grep_result = subprocess.run(  # noqa: S603 -- tmpdir is a controlled temp path
                    [
                        grep_bin,
                        "-rh",  # recursive, suppress filename prefix
                        "-oE",  # only-matching, extended regex
                        _GPL_GREP_PATTERN,
                        tmpdir,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return set(grep_result.stdout.splitlines())

        return await loop.run_in_executor(None, _clone_and_grep)
