"""CLI integration tests for the tpwalk Typer app.

Tests use typer.testing.CliRunner which captures stdout/stderr and captures
the exit code. All tests use mocked HTTP transport -- ZERO real network calls.
VerifyRunner is patched via tpwalk.verify.build_client and ScrapeRunner is
patched via tpwalk.scrape.build_client so neither runner ever creates a real
AsyncClient that could connect to the internet.

Per CLI-01, CLI-02, SCRP-01.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tpwalk.cli import app

_runner = CliRunner()


def test_verify_help() -> None:
    result = _runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--concurrency" in result.output


def test_verify_help_shows_data_dir() -> None:
    result = _runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--data-dir" in result.output


def test_scrape_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """scrape command runs end-to-end with mocked HTTP, prints summary, and writes output.

    Uses monkeypatch to replace build_client in tpwalk.scrape so no real HTTP
    client is created. The mock transport serves a minimal location picker and
    one region page with a direct productTree URL.

    Per SCRP-01, CLI-02, D-15.
    """
    # Minimal picker returning only "us"
    picker_html = '<html><body><a href="/us/">US</a></body></html>'
    # Region page with one direct productTree URL
    region_html = '<html><script>var productTree = {"1": [{"href": "https://static.tp-link.com/upload/gpl-code/2024/test_archive.tar.gz", "app_folder": "us", "model_name": "TestModel"}]};</script></html>'

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "choose-your-location" in url:
            return httpx.Response(200, text=picker_html)
        if "/us/support/gpl-code/" in url:
            return httpx.Response(200, text=region_html)
        if "/support/gpl-code/" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    def mock_build_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    import tpwalk.scrape as scrape_module

    monkeypatch.setattr(scrape_module, "build_client", mock_build_client)

    result = _runner.invoke(app, ["scrape", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, f"scrape exited with code {result.exit_code}: {result.output}"
    assert "Scrape complete" in result.output
    # Summary should contain digit counts
    assert "Unique" in result.output or any(ch.isdigit() for ch in result.output)

    # Verify output directory structure
    scrapes_dir = tmp_path / "scrapes"
    assert scrapes_dir.exists(), "scrapes/ directory was not created"
    subdirs = list(scrapes_dir.iterdir())
    assert len(subdirs) == 1, f"Expected 1 timestamped subdirectory, got {len(subdirs)}"
    txt_file = subdirs[0] / "regional_crawl.txt"
    assert txt_file.exists(), "regional_crawl.txt was not created in the run directory"


def test_scrape_help() -> None:
    """scrape --help shows the --data-dir option."""
    result = _runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    assert "--data-dir" in result.output


def test_bruteforce_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BRUT-01 smoke: bruteforce --dry-run exits 0 and prints the CLI-02 summary.

    Uses monkeypatch to replace build_client in tpwalk.bruteforce so no real
    HTTP client can be created. With --dry-run, no HEAD is issued anyway, but
    the mock ensures the test is safe even if that guarantee were relaxed.

    Per BRUT-01, CLI-02, D-09.
    """
    import tpwalk.bruteforce as bruteforce_module

    monkeypatch.setattr(
        bruteforce_module,
        "build_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: httpx.Response(404))),
    )

    result = _runner.invoke(
        app,
        ["bruteforce", "--dry-run", "--data-dir", str(tmp_path), "--strategy", "models", "--max-candidates", "5"],
    )
    assert result.exit_code == 0, f"bruteforce exited {result.exit_code}: {result.output}"


def test_bruteforce_summary_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI-02: the summary block labels must all appear in the output.

    Asserts that Strategy, Tier, Candidates checked, Hits, and Run dir are
    all present in the CLI output after a dry-run invocation.

    Per CLI-02, D-09.
    """
    import tpwalk.bruteforce as bruteforce_module

    monkeypatch.setattr(
        bruteforce_module,
        "build_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: httpx.Response(404))),
    )

    result = _runner.invoke(
        app,
        ["bruteforce", "--dry-run", "--data-dir", str(tmp_path), "--strategy", "models", "--max-candidates", "5"],
    )
    assert result.exit_code == 0, f"bruteforce failed: {result.output}"
    assert "Strategy" in result.output, f"'Strategy' missing from summary: {result.output}"
    assert "Tier" in result.output, f"'Tier' missing from summary: {result.output}"
    assert "Candidates checked" in result.output, f"'Candidates checked' missing from summary: {result.output}"
    assert "Hits" in result.output, f"'Hits' missing from summary: {result.output}"
    assert "Run dir" in result.output, f"'Run dir' missing from summary: {result.output}"


def test_bruteforce_strategy_validation() -> None:
    """BRUT-01 validation: --strategy bogus exits non-zero (Typer BadParameter on enum).

    Typer validates the Strategy StrEnum automatically; any value outside
    {dates, models, all} produces a non-zero exit code before the runner runs.

    Per T-05-12, Security Domain V5.
    """
    result = _runner.invoke(app, ["bruteforce", "--strategy", "bogus", "--dry-run"])
    assert result.exit_code != 0, f"Expected non-zero exit for bad --strategy; got: {result.exit_code}"


def test_bruteforce_data_dir_traversal_rejected() -> None:
    """BRUT-01 validation: --data-dir ../escape exits non-zero (path-traversal guard).

    The command rejects data_dir values containing '..' with typer.BadParameter
    so callers cannot escape the intended data root.

    Per T-05-13, Security Domain V5.
    """
    result = _runner.invoke(app, ["bruteforce", "--data-dir", "../escape", "--dry-run"])
    assert result.exit_code != 0, f"Expected non-zero exit for traversal data-dir; got: {result.exit_code}"


def test_bruteforce_help_flags() -> None:
    """D-09: all expected flags appear in bruteforce --help output."""
    result = _runner.invoke(app, ["bruteforce", "--help"])
    assert result.exit_code == 0
    for flag in ("--strategy", "--thorough", "--exhaustive", "--max-candidates", "--dry-run", "--concurrency", "--data-dir"):
        assert flag in result.output, f"Flag '{flag}' missing from bruteforce --help"


def test_no_args_shows_help() -> None:
    # no_args_is_help=True causes Typer to print help and exit 0 in real terminals,
    # but the CliRunner captures it as exit code 0 (help) or 2 (missing arg) depending
    # on Typer version. Accept both; the important assertion is that help is shown.
    result = _runner.invoke(app, [])
    assert result.exit_code in (0, 2)
    assert "verify" in result.output


def test_scrape_summary_includes_regional_crawl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-06 regression: scrape summary must include 'Regional crawl URLs' line.

    Prior to the fix, _source_labels omitted 'regional_crawl' so the primary
    TP-Link source was invisible in the CLI summary even though its counts were
    tracked in per_source_counts.
    """
    picker_html = '<html><body><a href="/us/">US</a></body></html>'
    region_html = '<html><script>var productTree = {"1": [{"href": "https://static.tp-link.com/upload/gpl-code/2024/wt06.tar.gz", "app_folder": "us", "model_name": "WR06Model"}]};</script></html>'

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "choose-your-location" in url:
            return httpx.Response(200, text=picker_html)
        if "/us/support/gpl-code/" in url:
            return httpx.Response(200, text=region_html)
        return httpx.Response(404)

    import tpwalk.scrape as scrape_module

    monkeypatch.setattr(scrape_module, "build_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    result = _runner.invoke(app, ["scrape", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, f"scrape failed: {result.output}"
    assert "Regional crawl URLs" in result.output, f"'Regional crawl URLs' must appear in summary; got:\n{result.output}"


def test_scrape_summary_github_skipped_when_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-06 regression: when GITHUB_TOKEN is absent, GitHub sources show 'skipped' not '0 unique (0 raw)'.

    The GitHub sources return set() when the token is absent — that produces
    (0, 0) in per_source_counts, which is indistinguishable from "ran and found nothing."
    The WR-06 fix detects the missing token and prints a distinct 'skipped' label.
    """
    picker_html = '<html><body><a href="/us/">US</a></body></html>'
    region_html = '<html><script>var productTree = {"1": []};</script></html>'

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "choose-your-location" in url:
            return httpx.Response(200, text=picker_html)
        if "/support/gpl-code/" in url:
            return httpx.Response(200, text=region_html)
        return httpx.Response(404)

    import tpwalk.scrape as scrape_module

    monkeypatch.setattr(scrape_module, "build_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)))
    # Ensure token is absent so GitHub sources skip
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    result = _runner.invoke(app, ["scrape", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, f"scrape failed: {result.output}"
    # GitHub sources must be marked skipped, not show "0 unique (0 raw)"
    assert "skipped" in result.output, f"Expected 'skipped' for GitHub sources when no token; got:\n{result.output}"
    assert "no GITHUB_TOKEN" in result.output, f"Expected 'no GITHUB_TOKEN' explanation in output; got:\n{result.output}"


def test_verify_with_empty_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify exits 0 with a summary showing 0 live URLs when no seed data exists.

    Uses monkeypatch to replace build_client in tpwalk.verify so no real HTTP
    client is created — the mock transport immediately returns 404 for any request,
    but because there are no URLs, head_check_all is never called at all.
    """

    def mock_build_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404)))

    import tpwalk.verify as verify_module

    monkeypatch.setattr(verify_module, "build_client", mock_build_client)

    result = _runner.invoke(app, ["verify", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    # Summary report must contain a "0" count for live (no URLs to verify)
    assert "0" in result.output
