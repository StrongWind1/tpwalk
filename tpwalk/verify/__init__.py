"""Verify pipeline for tpwalk — reads all discovered URLs, deduplicates, HEAD-checks, and writes results.

This subpackage orchestrates the full verification pipeline:
1. Read all .txt files from data/scrapes/ (seed/ + timestamped run directories)
2. Normalize and deduplicate URLs using url_normalize
3. HEAD-check each URL against the S3 origin (s3.amazonaws.com) for richer metadata
4. Batch-write five output files: verified.json, verified.txt, s5cmd_download.txt, dead.json, dead.txt

Per D-01 through D-07, VERF-01 through VERF-07, DIR-01 through DIR-05.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING

from tpwalk._client import build_client
from tpwalk._normalize import to_s5cmd_cp_line, to_s5cmd_url
from tpwalk.models import RunStats
from tpwalk.verify._head import head_check_all
from tpwalk.verify._reader import is_gpl_archive_url, read_all_txt

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

    from tpwalk.models import DeadEntry, VerifiedEntry


class VerifyRunner:
    """Orchestrates the full verify pipeline: read -> dedup -> HEAD -> write.

    Accepts data_dir and concurrency at construction time so the CLI layer can
    pass flags through without the runner knowing about typer or argparse.
    Progress injection is optional — pass progress=None for programmatic use
    without a terminal.

    Per VERF-01 through VERF-07, DIR-01 through DIR-05, D-01 through D-07.
    """

    def __init__(self, *, data_dir: str = "data", concurrency: int = 100) -> None:
        """Configure the verify runner.

        Args:
            data_dir: Root data directory. scrapes/ subdirectory is read from here.
                Output files (verified.json etc.) are written to this directory.
                Keyword-only per project conventions convention for >3-param functions.
            concurrency: Maximum concurrent HEAD requests to S3 origin.
                S3 imposes no rate limit; 100 is a safe default for most systems.

        """
        self._data_dir: Path = Path(data_dir)
        self._concurrency = concurrency

    async def run(
        self,
        *,
        progress: Progress | None = None,
        task_id: TaskID | None = None,
    ) -> RunStats:
        """Execute the full pipeline and batch-write all four output files.

        Pipeline steps (per D-05 — all in memory, batch-write at end):
        1. mkdir scrapes/ (no-op if exists)
        2. read_all_txt — discover and normalize all URLs from seed/ + timestamped dirs
        3. build_client — create HTTP/2 AsyncClient
        4. head_check_all — concurrent HEAD checks with semaphore bounding
        5. _write_results — batch-write verified.json, verified.txt, s5cmd_download.txt, dead.json, dead.txt

        The seed/ directory is never written to (DIR-01).

        Args:
            progress: Rich Progress instance for live display, or None.
            task_id: Rich task ID for the progress bar task, or None.

        Returns:
            RunStats with total_urls, unique_urls, live, and dead counts.

        """
        scrapes_dir = self._data_dir / "scrapes"
        scrapes_dir.mkdir(parents=True, exist_ok=True)

        # read_all_txt returns every URL discovered across all scrape sources.
        # The Common Crawl source queries a broad wildcard (static.tp-link.com/*)
        # and writes every crawled asset to common_crawl.txt — manuals, PDFs,
        # product images, software. That raw capture is kept on disk, but only
        # GPL-archive candidates are HEAD-checked so verified.txt stays the clean
        # GPL download list for s5cmd. is_gpl_archive_url is a deterministic gate.
        urls = {u for u in read_all_txt(scrapes_dir) if is_gpl_archive_url(u)}
        unique_count = len(urls)

        # Update the progress bar total now that we know how many URLs to check.
        # When progress is None (programmatic use, tests), this is a no-op.
        if progress is not None and task_id is not None:
            progress.update(task_id, total=unique_count)

        sem = asyncio.Semaphore(self._concurrency)
        async with build_client() as client:
            verified, dead = await head_check_all(
                urls,
                client=client,
                sem=sem,
                progress=progress,
                task_id=task_id,
            )

        self._write_results(verified, dead)

        return RunStats(
            total_urls=unique_count,  # We don't track raw line count separately; use unique as total
            unique_urls=unique_count,
            live=len(verified),
            dead=len(dead),
        )

    def _write_results(self, verified: list[VerifiedEntry], dead: list[DeadEntry]) -> None:
        """Batch-write all five output files to data_dir per D-05.

        Writes atomically at the end of the run (not streaming during HEAD checks).
        All five files are always written, even if one list is empty.

        Output files:
        - verified.json: flat JSON array of VerifiedEntry dicts (dataclasses.asdict)
        - verified.txt: one s3:// URL per line for s5cmd consumption (VERF-06, D-02)
        - s5cmd_download.txt: runnable `cp --if-size-differ` manifest for `s5cmd run`
        - dead.json: flat JSON array of DeadEntry dicts
        - dead.txt: one https:// URL per line (canonical form, D-02)

        Args:
            verified: List of confirmed-live VerifiedEntry results.
            dead: List of dead/error DeadEntry results.

        """
        data = self._data_dir
        data.mkdir(parents=True, exist_ok=True)

        # verified.json — flat array of D-06 field dicts
        data.joinpath("verified.json").write_text(
            json.dumps([dataclasses.asdict(v) for v in verified], indent=2),
            encoding="utf-8",
        )

        # verified.txt — s3:// URLs for s5cmd --no-sign-request input (VERF-06)
        verified_lines = "\n".join(to_s5cmd_url(v.url) for v in verified)
        data.joinpath("verified.txt").write_text(
            verified_lines + "\n" if verified_lines else "",
            encoding="utf-8",
        )

        # s5cmd_download.txt — runnable manifest for `s5cmd --no-sign-request run`.
        # One `cp --if-size-differ` per line; --if-size-differ makes re-runs
        # idempotent/resumable (skip files whose local size already matches). Sorted
        # for deterministic, diff-friendly output across runs.
        s5cmd_lines = "\n".join(sorted(to_s5cmd_cp_line(v.url) for v in verified))
        data.joinpath("s5cmd_download.txt").write_text(
            s5cmd_lines + "\n" if s5cmd_lines else "",
            encoding="utf-8",
        )

        # dead.json — flat array of DeadEntry field dicts
        data.joinpath("dead.json").write_text(
            json.dumps([dataclasses.asdict(d) for d in dead], indent=2),
            encoding="utf-8",
        )

        # dead.txt — canonical HTTPS URLs (one per line, D-02)
        dead_lines = "\n".join(d.url for d in dead)
        data.joinpath("dead.txt").write_text(
            dead_lines + "\n" if dead_lines else "",
            encoding="utf-8",
        )
