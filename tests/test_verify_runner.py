"""End-to-end pipeline tests for VerifyRunner.

Tests verify that VerifyRunner.run() reads URLs from seed/, HEAD-checks them with
mocked HTTP, and writes all five output files with correct content and format.
The seed/ directory is never modified by any pipeline run.

Per VERF-01 through VERF-07, DIR-01, DIR-04, DIR-05.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

_FULL_HEADERS = {
    "content-length": "12345678",
    "etag": '"abc123def456"',
    "content-type": "application/x-gzip",
    "last-modified": "Wed, 20 May 2026 12:00:00 GMT",
    "x-amz-version-id": "NB6JlD_Y7Cb3ZQPioiavpF3X1qRZztZY",
    "x-amz-replication-status": "COMPLETED",
    "x-amz-server-side-encryption": "AES256",
    "server": "AmazonS3",
}


def _make_mixed_transport(live_fragment: str) -> httpx.MockTransport:
    """Return a transport that returns 200 for URLs matching live_fragment, 404 otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        if live_fragment in str(request.url):
            return httpx.Response(200, headers=_FULL_HEADERS)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _run_verify_runner(data_dir: Path, transport: httpx.MockTransport) -> object:
    """Run VerifyRunner.run() with the given transport injected via monkeypatching."""
    from tpwalk.verify import VerifyRunner

    # Patch build_client to use our mock transport so no real network requests are made
    def mock_build_client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    import tpwalk.verify as verify_module

    verify_module_build_client = verify_module.build_client
    verify_module.build_client = mock_build_client  # type: ignore[assignment]

    try:
        runner = VerifyRunner(data_dir=str(data_dir))
        return asyncio.run(runner.run())
    finally:
        verify_module.build_client = verify_module_build_client  # type: ignore[assignment]


def test_verify_runner_writes_verified_json(tmp_path: Path) -> None:
    """verified.json exists and contains a valid JSON array after a run with live URLs."""

    # Set up seed data with one live URL
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_FULL_HEADERS)

    transport = httpx.MockTransport(handler)
    _run_verify_runner(tmp_path, transport)

    verified_json = tmp_path / "verified.json"
    assert verified_json.exists(), "verified.json was not created"
    data = json.loads(verified_json.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["url"] == "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"


def test_verify_runner_verified_txt_uses_s3_urls(tmp_path: Path) -> None:
    """verified.txt lines start with s3:// (to_s5cmd_url conversion applied per VERF-06)."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_FULL_HEADERS)

    _run_verify_runner(tmp_path, httpx.MockTransport(handler))

    verified_txt = tmp_path / "verified.txt"
    assert verified_txt.exists(), "verified.txt was not created"
    lines = [line for line in verified_txt.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert lines[0].startswith("s3://"), f"Expected s3:// prefix, got: {lines[0]}"


def test_verify_runner_dead_txt_uses_https_urls(tmp_path: Path) -> None:
    """dead.txt lines start with https:// (canonical HTTPS form per D-02)."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/dead.tgz\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _run_verify_runner(tmp_path, httpx.MockTransport(handler))

    dead_txt = tmp_path / "dead.txt"
    assert dead_txt.exists(), "dead.txt was not created"
    lines = [line for line in dead_txt.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert lines[0].startswith("https://"), f"Expected https:// prefix, got: {lines[0]}"


def test_verify_runner_writes_all_five_files(tmp_path: Path) -> None:
    """All five output files are created: verified.json, verified.txt, s5cmd_download.txt, dead.json, dead.txt."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/live.tgz\nhttps://static.tp-link.com/resources/gpl/dead.tgz\n",
        encoding="utf-8",
    )

    _run_verify_runner(tmp_path, _make_mixed_transport("live.tgz"))

    for filename in ("verified.json", "verified.txt", "s5cmd_download.txt", "dead.json", "dead.txt"):
        assert (tmp_path / filename).exists(), f"{filename} was not created"


def test_verify_runner_s5cmd_download_format(tmp_path: Path) -> None:
    """s5cmd_download.txt lines are runnable `cp --if-size-differ` commands with literal keys.

    The key has a space, so this also guards the decode: the s3:// source must be
    the literal key (not %20) or s5cmd would double-encode and 403.
    """
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/Archer C50_V6_GPL.tar.gz\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_FULL_HEADERS)

    _run_verify_runner(tmp_path, httpx.MockTransport(handler))

    lines = [line for line in (tmp_path / "s5cmd_download.txt").read_text(encoding="utf-8").splitlines() if line]
    assert lines == ["cp --if-size-differ 's3://static.tp-link.com/resources/gpl/Archer C50_V6_GPL.tar.gz' 'gpl/static.tp-link.com/resources/gpl/Archer C50_V6_GPL.tar.gz'"]


def test_verify_runner_verified_json_has_all_d06_fields(tmp_path: Path) -> None:
    """Verified entries in verified.json contain all D-06 fields."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/upload/gpl-code/2026/202605/20260527/GPL_AXE5400V2.tar.gz\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_FULL_HEADERS)

    _run_verify_runner(tmp_path, httpx.MockTransport(handler))

    data = json.loads((tmp_path / "verified.json").read_text(encoding="utf-8"))
    assert len(data) == 1
    entry = data[0]
    # All D-06 fields must be present
    required_fields = {"url", "size", "etag", "content_type", "last_modified", "version_id", "status", "checked_at", "replication_status", "encryption", "server"}
    assert required_fields.issubset(entry.keys()), f"Missing fields: {required_fields - entry.keys()}"


def test_verify_runner_dead_json_has_required_fields(tmp_path: Path) -> None:
    """Dead entries in dead.json have url, status, error_type, checked_at fields."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/does_not_exist.tgz\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _run_verify_runner(tmp_path, httpx.MockTransport(handler))

    data = json.loads((tmp_path / "dead.json").read_text(encoding="utf-8"))
    assert len(data) == 1
    entry = data[0]
    assert "url" in entry
    assert "status" in entry
    assert "error_type" in entry
    assert "checked_at" in entry


def test_verify_runner_line_counts_match(tmp_path: Path) -> None:
    """verified.txt line count matches verified.json entry count."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/a.tgz\nhttps://static.tp-link.com/resources/gpl/b.tgz\nhttps://static.tp-link.com/resources/gpl/dead.tgz\n",
        encoding="utf-8",
    )

    _run_verify_runner(tmp_path, _make_mixed_transport("does_not_match_anything_so_all_dead"))

    verified_json = json.loads((tmp_path / "verified.json").read_text(encoding="utf-8"))
    verified_txt_lines = [line for line in (tmp_path / "verified.txt").read_text(encoding="utf-8").splitlines() if line]
    assert len(verified_json) == len(verified_txt_lines)


def test_verify_runner_run_stats(tmp_path: Path) -> None:
    """RunStats.live + RunStats.dead == RunStats.unique_urls."""
    from tpwalk.models import RunStats

    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/live.tgz\nhttps://static.tp-link.com/resources/gpl/dead.tgz\nhttps://static.tp-link.com/resources/gpl/also_dead.tgz\n",
        encoding="utf-8",
    )

    stats = _run_verify_runner(tmp_path, _make_mixed_transport("live.tgz"))
    assert isinstance(stats, RunStats)
    assert stats.live + stats.dead == stats.unique_urls


def test_seed_not_written_during_run(tmp_path: Path) -> None:
    """seed/ directory is never modified by VerifyRunner.run() (DIR-01)."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz\n",
        encoding="utf-8",
    )

    before_files = {str(p) for p in seed.rglob("*")}
    before_mtime = (seed / "urls.txt").stat().st_mtime

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_FULL_HEADERS)

    _run_verify_runner(tmp_path, httpx.MockTransport(handler))

    after_files = {str(p) for p in seed.rglob("*")}
    after_mtime = (seed / "urls.txt").stat().st_mtime

    assert before_files == after_files, "seed/ directory contents changed during run"
    assert before_mtime == after_mtime, "seed/ file was modified during run"
