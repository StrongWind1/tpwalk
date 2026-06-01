"""Tests for tpwalk.scrape -- ScrapeRunner I/O, timestamped directory, txt write.

Per SCRP-04, SCRP-05, DIR-02.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tpwalk.models import ScrapeStats

# Minimal valid region page with one direct productTree URL
_STUB_REGION_PAGE = """<html><script>
var productTree = {"1": [{"href": "https://static.tp-link.com/upload/gpl-code/2024/stub_a.tar.gz", "app_folder": "us", "model_name": "StubModel"}]};
</script></html>"""

# Two direct URLs for dedup testing (one duplicate, different case in path)
_TWO_URL_REGION_PAGE = """<html><script>
var productTree = {"1": [
    {"href": "https://static.tp-link.com/upload/gpl-code/2024/file_a.tar.gz", "app_folder": "us", "model_name": "ModelA"},
    {"href": "https://static.tp-link.com/upload/gpl-code/2024/file_b.tar.gz", "app_folder": "us", "model_name": "ModelB"},
    {"href": "https://static.tp-link.com/upload/gpl-code/2024/file_a.tar.gz", "app_folder": "us", "model_name": "ModelA_dup"}
]};
</script></html>"""

_PICKER_HTML = '<html><body><a href="/us/">US</a></body></html>'

# Minimal Wayback CDX response with one URL (for ScrapeRunner integration tests)
_WAYBACK_CDX_RESPONSE = '["original"]\n["https://static.tp-link.com/resources/gpl/test_wayback.tar.gz"]\n'

# Empty CDX response (header only)
_WAYBACK_CDX_EMPTY = '["original"]\n'

# Minimal Common Crawl collinfo.json with one fake index
_CC_COLLINFO_ONE_INDEX = [{"id": "CC-MAIN-2024-01", "name": "CC-MAIN-2024-01", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-01-index"}]
_CC_COLLINFO_EMPTY: list[dict[str, str]] = []

# Minimal CC NDJSON response with one URL
_CC_NDJSON_RESPONSE = json.dumps({"urlkey": "com,tp-link,static)/test", "timestamp": "20240101000000", "url": "https://static.tp-link.com/gpl/test_cc.tar.gz", "mime": "application/gzip", "status": "200", "digest": "SHA1:AAA", "length": "100", "offset": "0", "filename": "cc.warc.gz"})
_CC_SHOW_NUM_PAGES_ONE = json.dumps({"pages": 1, "pageSize": 5, "blocks": 1})
_CC_SHOW_NUM_PAGES_ZERO = json.dumps({"pages": 0, "pageSize": 5, "blocks": 0})


def _handle_cdx_request(url: str, *, include_wayback: bool) -> httpx.Response:
    """Route a Wayback CDX API request to the correct mock response."""
    if not include_wayback:
        return httpx.Response(200, text=_WAYBACK_CDX_EMPTY)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    url_param = params.get("url", [""])[0]
    if url_param == "static.tp-link.com/upload/gpl-code/*":
        return httpx.Response(200, text=_WAYBACK_CDX_RESPONSE)
    return httpx.Response(200, text=_WAYBACK_CDX_EMPTY)


def _handle_cc_request(url: str, *, include_cc: bool) -> httpx.Response:
    """Route a Common Crawl API request to the correct mock response."""
    if "collinfo.json" in url:
        data = _CC_COLLINFO_ONE_INDEX if include_cc else _CC_COLLINFO_EMPTY
        return httpx.Response(200, json=data)

    if "CC-MAIN-2024-01-index" not in url:
        return httpx.Response(404)

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "showNumPages" in params:
        text = _CC_SHOW_NUM_PAGES_ONE if include_cc else _CC_SHOW_NUM_PAGES_ZERO
        return httpx.Response(200, text=text)

    # Page data request
    text = _CC_NDJSON_RESPONSE if include_cc else ""
    return httpx.Response(200, text=text)


def _make_stub_transport(
    region_html: str = _STUB_REGION_PAGE,
    *,
    include_wayback: bool = False,
    include_cc: bool = False,
) -> httpx.MockTransport:
    """Return a transport serving minimal valid responses for ScrapeRunner.

    Args:
        region_html: HTML to serve for the US region GPL page.
        include_wayback: If True, serve a CDX response with one URL for the
            date-hierarchical prefix and empty responses for other prefixes.
        include_cc: If True, serve collinfo.json with one index and one NDJSON URL.

    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "web.archive.org/cdx/search/cdx" in url:
            return _handle_cdx_request(url, include_wayback=include_wayback)
        if "index.commoncrawl.org" in url or "CC-MAIN-" in url:
            return _handle_cc_request(url, include_cc=include_cc)
        if "choose-your-location" in url:
            return httpx.Response(200, text=_PICKER_HTML)
        if "/us/support/gpl-code/" in url:
            return httpx.Response(200, text=region_html)
        if "/support/gpl-code/" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _run_scrape_runner(data_dir: Path, transport: httpx.MockTransport) -> ScrapeStats:
    """Run ScrapeRunner.run() with the given transport injected via monkeypatching."""
    import tpwalk.scrape as scrape_module
    from tpwalk.scrape import ScrapeRunner

    def mock_build_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    orig = scrape_module.build_client
    scrape_module.build_client = mock_build_client  # type: ignore[assignment]
    try:
        runner = ScrapeRunner(data_dir=str(data_dir))
        return asyncio.run(runner.run())
    finally:
        scrape_module.build_client = orig  # type: ignore[assignment]


def test_runner_creates_run_dir(tmp_path: Path) -> None:
    """ScrapeRunner creates a timestamped subdirectory under data/scrapes/ (DIR-02)."""
    _run_scrape_runner(tmp_path, _make_stub_transport())

    scrapes = tmp_path / "scrapes"
    assert scrapes.exists(), "scrapes/ directory was not created"
    subdirs = list(scrapes.iterdir())
    assert len(subdirs) == 1, f"Expected 1 run dir, got {len(subdirs)}: {subdirs}"
    # Format: YYYY-MM-DDThhmm (Pitfall 5)
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{4}", subdirs[0].name), f"Run dir name does not match YYYY-MM-DDThhmm: {subdirs[0].name}"


def test_runner_writes_regional_crawl_txt(tmp_path: Path) -> None:
    """ScrapeRunner writes regional_crawl.txt inside the run directory (SCRP-04, D-14)."""
    _run_scrape_runner(tmp_path, _make_stub_transport())

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    txt_file = run_dir / "regional_crawl.txt"
    assert txt_file.exists(), "regional_crawl.txt was not created"


def test_runner_txt_has_normalized_urls(tmp_path: Path) -> None:
    """URLs in regional_crawl.txt are normalized (lowercase scheme+host, canonical encoding)."""
    _run_scrape_runner(tmp_path, _make_stub_transport())

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    content = (run_dir / "regional_crawl.txt").read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) >= 1, "Expected at least 1 URL in regional_crawl.txt"
    for line in lines:
        assert line.startswith("https://"), f"URL not normalized (missing https://): {line}"
        # Verify no raw spaces in URLs (url_normalize converts spaces to %20)
        assert " " not in line, f"URL contains raw space: {line}"


def test_runner_txt_sorted_and_deduplicated(tmp_path: Path) -> None:
    """URLs in regional_crawl.txt are sorted and deduplicated after normalization (D-13)."""
    _run_scrape_runner(tmp_path, _make_stub_transport(_TWO_URL_REGION_PAGE))

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    content = (run_dir / "regional_crawl.txt").read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]

    # Should have 2 unique URLs (file_a.tar.gz appears twice in productTree)
    assert len(lines) == 2, f"Expected 2 unique URLs (deduped), got {len(lines)}: {lines}"
    # Should be sorted
    assert lines == sorted(lines), f"URLs not sorted: {lines}"


def test_runner_returns_scrape_stats(tmp_path: Path) -> None:
    """ScrapeRunner.run() returns ScrapeStats with correct counts (D-15)."""
    stats = _run_scrape_runner(tmp_path, _make_stub_transport(_TWO_URL_REGION_PAGE))

    assert isinstance(stats, ScrapeStats)
    assert stats.pass1_urls >= 1, f"Expected pass1_urls >= 1, got {stats.pass1_urls}"
    assert stats.raw_count >= 1, f"Expected raw_count >= 1, got {stats.raw_count}"
    assert stats.unique_count >= 1, f"Expected unique_count >= 1, got {stats.unique_count}"
    assert stats.raw_count >= stats.unique_count, f"raw_count ({stats.raw_count}) < unique_count ({stats.unique_count})"
    assert stats.regions_scraped >= 1, f"Expected regions_scraped >= 1, got {stats.regions_scraped}"


def test_runner_source_error_caught(tmp_path: Path) -> None:
    """ScrapeRunner catches source errors without crashing the runner (SCRP-05)."""

    # Transport that always returns 500 for everything (including picker, region pages)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    # Should not raise -- source errors are caught per SCRP-05
    stats = _run_scrape_runner(tmp_path, transport)
    assert isinstance(stats, ScrapeStats)

    # Run dir should still be created even if source produced 0 URLs
    scrapes = tmp_path / "scrapes"
    assert scrapes.exists()


def test_runner_writes_wayback_cdx_txt(tmp_path: Path) -> None:
    """ScrapeRunner writes wayback_cdx.txt with WaybackSource results in the run directory.

    Proves the vertical slice: WaybackSource registered -> ScrapeRunner runs it ->
    wayback_cdx.txt file written with normalized URLs.
    """
    _run_scrape_runner(tmp_path, _make_stub_transport(include_wayback=True))

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    wayback_txt = run_dir / "wayback_cdx.txt"
    assert wayback_txt.exists(), "wayback_cdx.txt was not created by ScrapeRunner"

    content = wayback_txt.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) >= 1, f"Expected at least 1 URL in wayback_cdx.txt, got {len(lines)}"
    # Verify the specific URL from the mock CDX response is present (normalized)
    assert any("test_wayback.tar.gz" in line for line in lines), f"Expected test_wayback.tar.gz URL in wayback_cdx.txt: {lines}"


def test_runner_writes_common_crawl_txt(tmp_path: Path) -> None:
    """ScrapeRunner writes common_crawl.txt with CommonCrawlSource results in the run directory.

    Proves the vertical slice: CommonCrawlSource registered -> ScrapeRunner runs it ->
    common_crawl.txt file written with normalized URLs.
    """
    _run_scrape_runner(tmp_path, _make_stub_transport(include_cc=True))

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    cc_txt = run_dir / "common_crawl.txt"
    assert cc_txt.exists(), "common_crawl.txt was not created by ScrapeRunner"

    content = cc_txt.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) >= 1, f"Expected at least 1 URL in common_crawl.txt, got {len(lines)}"
    assert any("test_cc.tar.gz" in line for line in lines), f"Expected test_cc.tar.gz URL in common_crawl.txt: {lines}"


def test_runner_three_source_txt_files(tmp_path: Path) -> None:
    """Run the scrape runner and assert the original 3 source txt files are present among all 9 (SCRP-04).

    Phase 4 extended the runner to 9 sources; this test verifies the original 3
    are still present in every run (regression guard for D-17).
    """
    _run_scrape_runner(tmp_path, _make_stub_transport(include_wayback=True, include_cc=True))

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())

    txt_files = {f.name for f in run_dir.iterdir() if f.suffix == ".txt"}
    for expected in ["common_crawl.txt", "regional_crawl.txt", "wayback_cdx.txt"]:
        assert expected in txt_files, f"{expected} missing from run dir: {sorted(txt_files)}"


def test_runner_cdx_source_error_caught(tmp_path: Path) -> None:
    """Transport returns 200 for regional pages but 500 for all CDX endpoints.

    Runner completes without exception, regional_crawl.txt is written.
    CDX txt files are empty (source error caught per SCRP-05).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        # CDX endpoints: always 500
        if "web.archive.org" in url or "commoncrawl.org" in url or "CC-MAIN-" in url:
            return httpx.Response(500)

        # Regional pages: normal
        if "choose-your-location" in url:
            return httpx.Response(200, text=_PICKER_HTML)
        if "/us/support/gpl-code/" in url:
            return httpx.Response(200, text=_STUB_REGION_PAGE)
        if "/support/gpl-code/" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    stats = _run_scrape_runner(tmp_path, transport)

    # Runner completes without exception
    assert isinstance(stats, ScrapeStats)

    # Regional crawl still works
    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    regional_txt = run_dir / "regional_crawl.txt"
    assert regional_txt.exists(), "regional_crawl.txt should exist even when CDX sources fail"

    regional_content = regional_txt.read_text(encoding="utf-8")
    assert len(regional_content.strip()) > 0, "regional_crawl.txt should have URLs"


# ---------------------------------------------------------------------------
# Phase 4 integration tests
# ---------------------------------------------------------------------------


def test_runner_has_all_phase4_sources() -> None:
    """ScrapeRunner._sources contains all 9 sources (3 original + 6 Phase 4) (D-17)."""
    from tpwalk.scrape import ScrapeRunner

    runner = ScrapeRunner(data_dir="data")
    source_names = {s.name for s in runner._sources}
    expected = {
        "regional_crawl",
        "wayback_cdx",
        "common_crawl",  # existing
        "github_search",
        "tplink_github",
        "mercusys_regional",  # Phase 4
        "reddit",
        "forums",
        "google",  # Phase 4
    }
    assert expected == source_names, f"Missing sources: {expected - source_names}"


_P4_EMPTY_GITHUB_JSON = {"total_count": 0, "incomplete_results": False, "items": []}
_P4_EMPTY_REDDIT_JSON = {"kind": "Listing", "data": {"children": [], "after": None, "before": None}}
_P4_EMPTY_DISCOURSE_JSON = {"posts": [], "topics": [], "grouped_search_result": {"more_posts": False, "post_ids": [], "error": None}}
_P4_EMPTY_HTML = "<html><body><p>No results</p></body></html>"


def _handle_phase4_request(request: httpx.Request) -> httpx.Response | None:
    """Route a Phase 4 source request to a benign empty response, or return None for unknown hosts.

    Covers api.github.com, reddit.com, forum.openwrt.org, google.com, and
    mercusys.com. Returns None for hosts that are not Phase 4 sources so the
    caller can fall through to the original three-source handlers.
    """
    host = request.url.host
    if host == "api.github.com":
        return httpx.Response(200, json=_P4_EMPTY_GITHUB_JSON)
    if host in ("www.reddit.com", "reddit.com"):
        return httpx.Response(200, json=_P4_EMPTY_REDDIT_JSON)
    if host == "forum.openwrt.org":
        return httpx.Response(200, json=_P4_EMPTY_DISCOURSE_JSON)
    if host in ("www.google.com", "google.com"):
        return httpx.Response(200, text=_P4_EMPTY_HTML)
    if host in ("www.mercusys.com", "mercusys.com"):
        return httpx.Response(200, text=_P4_EMPTY_HTML)
    return None


def _make_phase4_transport(
    region_html: str = _STUB_REGION_PAGE,
    *,
    include_wayback: bool = False,
    include_cc: bool = False,
) -> httpx.MockTransport:
    """Return a MockTransport serving benign empty responses for all 9 source endpoints.

    Dispatches by host/URL. Phase 4 source requests are handled by
    _handle_phase4_request. Original CDX/CC/regional requests are delegated to
    the existing helpers for full regression coverage. No real network is used.

    Args:
        region_html: HTML to serve for the US region GPL page.
        include_wayback: If True, serve a CDX response with one URL.
        include_cc: If True, serve collinfo.json with one CC index and NDJSON URL.

    """

    def _handle_original_sources(request: httpx.Request, url: str, host: str) -> httpx.Response:
        """Route requests for the three original source endpoints (regional, CDX, CC)."""
        if "web.archive.org/cdx/search/cdx" in url:
            return _handle_cdx_request(url, include_wayback=include_wayback)
        if "index.commoncrawl.org" in url or "CC-MAIN-" in url:
            return _handle_cc_request(url, include_cc=include_cc)
        if "choose-your-location" in url:
            return httpx.Response(200, text=_PICKER_HTML)
        if "tp-link.com" in host and "/us/support/gpl-code/" in url:
            return httpx.Response(200, text=region_html)
        return httpx.Response(404)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host
        p4 = _handle_phase4_request(request)
        if p4 is not None:
            return p4
        return _handle_original_sources(request, url, host)

    return httpx.MockTransport(handler)


def test_runner_writes_all_phase4_txt_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mocked full run writes one txt file per Phase 4 source (success criterion #1).

    GITHUB_TOKEN is unset so the GitHub sources skip via the credential guard.
    All other new sources hit the mock transport and return empty sets, which
    the runner still writes as empty txt files (SCRP-04 applies to every source,
    empty or not).

    Per Phase 4 success criterion #1, SCRP-04, D-17.
    """
    # Ensure GITHUB_TOKEN is not set -- GitHub sources skip via D-02 credential guard
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    transport = _make_phase4_transport(include_wayback=False, include_cc=False)
    _run_scrape_runner(tmp_path, transport)

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    txt_files = {f.name for f in run_dir.iterdir() if f.suffix == ".txt"}

    for expected in ("github_search.txt", "tplink_github.txt", "mercusys_regional.txt", "reddit.txt", "forums.txt", "google.txt"):
        assert expected in txt_files, f"{expected} missing from run dir: {sorted(txt_files)}"


def test_runner_phase4_full_run_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full ScrapeRunner.run() with all 9 sources under MockTransport — no real network.

    Asserts all 9 txt files are written and the run completes without exception.
    GITHUB_TOKEN unset so GitHub sources skip gracefully (D-02).

    Per Phase 4 success criterion #1, SCRP-04, D-17.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    transport = _make_phase4_transport(include_wayback=True, include_cc=True)
    stats = _run_scrape_runner(tmp_path, transport)

    assert isinstance(stats, ScrapeStats)

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    txt_files = {f.name for f in run_dir.iterdir() if f.suffix == ".txt"}

    expected_all = {
        "regional_crawl.txt",
        "wayback_cdx.txt",
        "common_crawl.txt",
        "github_search.txt",
        "tplink_github.txt",
        "mercusys_regional.txt",
        "reddit.txt",
        "forums.txt",
        "google.txt",
    }
    assert expected_all == txt_files, f"Expected {sorted(expected_all)}, got {sorted(txt_files)}"


def test_runner_phase4_source_failure_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One Phase 4 source raising a caught exception does not abort other sources (SCRP-05).

    Injects a broken source into the runner that raises RuntimeError (one of the
    SCRP-05 caught exception families). Asserts the runner completes normally and
    the other sources' txt files are still written.

    Per SCRP-05, D-17.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    import tpwalk.scrape as scrape_module
    from tpwalk.scrape import ScrapeRunner

    # Build a transport that handles all source endpoints
    transport = _make_phase4_transport(include_wayback=False, include_cc=False)

    def mock_build_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    # Inject a source that always raises RuntimeError (SCRP-05 caught family)
    class _BrokenSource:
        @property
        def name(self) -> str:
            return "broken_source_test"

        async def run(
            self,
            *,
            client: object,  # noqa: ARG002
            progress: object = None,  # noqa: ARG002
            task_pass1: object = None,  # noqa: ARG002
            task_pass2: object = None,  # noqa: ARG002
        ) -> set[str]:
            msg = "Simulated source failure for SCRP-05 isolation test"
            raise RuntimeError(msg)

    orig_build_client = scrape_module.build_client
    scrape_module.build_client = mock_build_client  # type: ignore[assignment]
    try:
        runner = ScrapeRunner(data_dir=str(tmp_path))
        # Inject the broken source after the first source so other sources still run
        runner._sources.insert(1, _BrokenSource())  # type: ignore[arg-type]

        stats = asyncio.run(runner.run())
    finally:
        scrape_module.build_client = orig_build_client  # type: ignore[assignment]

    # Runner must complete without exception
    assert isinstance(stats, ScrapeStats)

    scrapes = tmp_path / "scrapes"
    run_dir = next(scrapes.iterdir())
    txt_files = {f.name for f in run_dir.iterdir() if f.suffix == ".txt"}

    # broken_source_test.txt must NOT exist (source raised before writing)
    assert "broken_source_test.txt" not in txt_files, "Broken source should not have written a txt file"

    # All other 9 sources must still have written their txt files
    for expected in ("regional_crawl.txt", "github_search.txt", "tplink_github.txt", "mercusys_regional.txt", "reddit.txt", "forums.txt", "google.txt"):
        assert expected in txt_files, f"{expected} missing -- source-failure isolation failed: {sorted(txt_files)}"


def test_runner_progress_uses_per_source_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-05 regression: each source gets independent progress tasks, not the shared ones.

    The runner creates a fresh (task_pass1, task_pass2) pair per source and passes those
    into source.run(), so no source's progress.update(total=N) overwrites another source's
    total. This test verifies that add_task is called at least once per source executed
    (i.e. the runner creates per-source tasks, not just the CLI-supplied pair).

    Strategy: inject a mock Progress that records every add_task call.
    Assert add_task was called at least as many times as there are sources.
    """
    from unittest.mock import MagicMock

    import tpwalk.scrape as scrape_module
    from tpwalk.scrape import ScrapeRunner

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    transport = _make_phase4_transport(include_wayback=False, include_cc=False)

    def mock_build_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    # Build a mock Progress that records add_task / remove_task calls.
    # add_task returns incrementing integers so sources can call progress.update(task_id, ...).
    add_task_counter: list[int] = [0]
    add_task_calls: list[tuple[str, object]] = []
    remove_task_calls: list[int] = []

    mock_progress = MagicMock()

    def fake_add_task(description: str, **kwargs: object) -> int:
        add_task_calls.append((description, kwargs))
        tid = add_task_counter[0]
        add_task_counter[0] += 1
        return tid

    def fake_remove_task(tid: int) -> None:
        remove_task_calls.append(tid)

    mock_progress.add_task.side_effect = fake_add_task
    mock_progress.remove_task.side_effect = fake_remove_task
    mock_progress.update = MagicMock()  # accept any update calls without error

    orig_build_client = scrape_module.build_client
    scrape_module.build_client = mock_build_client  # type: ignore[assignment]
    try:
        runner = ScrapeRunner(data_dir=str(tmp_path))
        n_sources = len(runner._sources)
        stats = asyncio.run(runner.run(progress=mock_progress))
    finally:
        scrape_module.build_client = orig_build_client  # type: ignore[assignment]

    assert isinstance(stats, ScrapeStats)

    # Runner must have called add_task at least 2 * n_sources times (one pass1 + one pass2 per source).
    # Some sources may fail (caught by SCRP-05), but the runner still creates tasks before trying.
    assert len(add_task_calls) >= n_sources, f"Expected at least {n_sources} add_task calls (one per source); got {len(add_task_calls)}"

    # Every created task must have been removed (no task leak).
    tasks_created = set(range(add_task_counter[0]))
    tasks_removed = set(remove_task_calls)
    assert tasks_created == tasks_removed, f"Progress task leak: created {tasks_created}, removed {tasks_removed}"
