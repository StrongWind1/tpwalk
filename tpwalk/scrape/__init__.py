"""Scrape pipeline for tpwalk -- discovers GPL archive URLs from all regional sources.

This subpackage orchestrates the full scrape pipeline:
1. Build the timestamped run directory under data/scrapes/ (DIR-02)
2. Run each ScrapeSource concurrently under a shared AsyncClient
3. Normalize and deduplicate URLs via url_normalize per source
4. Write one .txt file per source using the source's name property (SCRP-04)

Per D-11, D-12, D-13, D-14, D-15, SCRP-01 through SCRP-05.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

from tpwalk._client import build_client
from tpwalk._normalize import url_normalize
from tpwalk.models import ScrapeStats
from tpwalk.scrape._common_crawl import CommonCrawlSource
from tpwalk.scrape._forums import ForumSource
from tpwalk.scrape._github import GitHubSearchSource, TPLinkGitHubSource
from tpwalk.scrape._google import GoogleSource
from tpwalk.scrape._mercusys import MercusysRegionalSource
from tpwalk.scrape._model_sweep import ModelSweepSource
from tpwalk.scrape._reddit import RedditSource
from tpwalk.scrape._regional import RegionalSource
from tpwalk.scrape._wayback import WaybackSource

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)


@runtime_checkable
class ScrapeSource(Protocol):
    """Structural interface for all passive discovery sources.

    Each source has a name (used as the output filename stem) and an async
    run() method that returns a raw set of URLs. The ScrapeRunner handles all
    I/O; sources never write files directly (D-12).

    Per D-11. Protocol (PEP 544) chosen over ABC to allow structural subtyping
    -- future sources in other packages can conform without inheriting.
    """

    @property
    def name(self) -> str:
        """Filename stem: f"{self.name}.txt" produces e.g. "regional_crawl.txt"."""
        ...

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,
    ) -> set[str]:
        """Execute the source and return a raw set of discovered URLs."""
        ...


class ScrapeRunner:
    """Orchestrates all ScrapeSource instances: mkdir -> run sources -> normalize -> write.

    Accepts data_dir at construction time so the CLI layer can pass --data-dir
    without the runner knowing about typer. Progress injection is optional.

    Per D-11, D-12, SCRP-01.
    """

    def __init__(self, *, data_dir: str = "data", model_sweep: bool = False, sweep_max_models: int | None = None) -> None:
        """Configure the scrape runner.

        Args:
            data_dir: Root data directory. Writes scrapes/ subdirectory here.
            model_sweep: When True, append the heavy phppage model-wordlist sweep
                (ModelSweepSource) -- opt-in because it issues thousands of requests
                to www.tp-link.com. Wired to the CLI's `scrape --model-sweep` flag.
            sweep_max_models: Optional cap on candidate models for the sweep.

        """
        self._data_dir: Path = Path(data_dir)
        self._sources: list[ScrapeSource] = [
            RegionalSource(),
            WaybackSource(),
            CommonCrawlSource(),
            GitHubSearchSource(),  # Phase 4
            TPLinkGitHubSource(),  # Phase 4
            MercusysRegionalSource(),  # Phase 4
            RedditSource(),  # Phase 4
            ForumSource(),  # Phase 4
            GoogleSource(),  # Phase 4
        ]
        if model_sweep:
            self._sources.append(ModelSweepSource(max_models=sweep_max_models))
        self.per_source_counts: dict[str, tuple[int, int]] = {}

    async def run(
        self,
        *,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,  # noqa: ARG002 -- superseded by per-source tasks (WR-05)
        task_pass2: TaskID | None = None,  # noqa: ARG002 -- superseded by per-source tasks (WR-05)
    ) -> ScrapeStats:
        """Execute all sources and write per-source txt files.

        Pipeline:
        1. Create timestamped run directory (DIR-02, Pitfall 5)
        2. Build shared AsyncClient via build_client()
        3. For each source: run, normalize URLs, write txt file (SCRP-04)
        4. Return ScrapeStats with summary counts (D-15)

        Source-level exceptions are caught and logged without aborting the
        entire runner (SCRP-05).

        Progress isolation (WR-05): each source receives its own ephemeral
        task_pass1/task_pass2 pair created and removed by the runner rather than
        the shared task IDs supplied by the CLI. This prevents each source's
        progress.update(..., total=N) call from clobbering the previous source's
        total, which would produce a nonsensical MofNCompleteColumn display.
        The caller-supplied task_pass1/task_pass2 are accepted for Protocol
        compatibility but are superseded by the per-source tasks created here.

        Args:
            progress: Rich Progress instance for live display, or None.
            task_pass1: Accepted for Protocol compatibility; superseded by per-source tasks (WR-05).
            task_pass2: Accepted for Protocol compatibility; superseded by per-source tasks (WR-05).

        Returns:
            ScrapeStats with summary counts from the run.

        """
        # DIR-02: timestamped run directory -- use D-03 format, not date-only (Pitfall 5)
        stamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H%M")
        run_dir = self._data_dir / "scrapes" / stamp
        run_dir.mkdir(parents=True, exist_ok=True)

        total_raw = 0
        total_unique = 0
        pass1_urls = 0
        pass2_urls = 0
        regions_scraped = 0
        regions_failed = 0

        async with build_client() as client:
            for source in self._sources:
                # WR-05: create isolated per-source tasks so each source's total update
                # does not clobber the previous source's progress bar display.
                src_task1: TaskID | None = None
                src_task2: TaskID | None = None
                if progress is not None:
                    src_task1 = progress.add_task(f"[dim]{source.name}[/] pass 1", total=None, visible=False)
                    src_task2 = progress.add_task(f"[dim]{source.name}[/] pass 2", total=None, visible=False)

                source_failed = False
                raw_urls: set[str] = set()  # sentinel; replaced on success
                try:
                    raw_urls = await source.run(
                        client=client,
                        progress=progress,
                        task_pass1=src_task1,
                        task_pass2=src_task2,
                    )
                except (  # SCRP-05: catch known exception families to not abort the runner
                    httpx.RequestError,
                    httpx.HTTPStatusError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                ):
                    _log.exception("Source %s failed", source.name)
                    source_failed = True
                finally:
                    # Remove per-source tasks after each source to avoid task accumulation.
                    # Best-effort: task may already be gone if the source removed it internally.
                    for _task in (src_task1, src_task2):
                        if progress is not None and _task is not None:
                            with contextlib.suppress(Exception):
                                progress.remove_task(_task)
                if source_failed:
                    continue

                # Normalize and deduplicate before writing (D-13)
                normalized = {url_normalize(u) for u in raw_urls} - {""}
                total_raw += len(raw_urls)
                total_unique += len(normalized)

                # Write per-source txt file (SCRP-04, D-14)
                txt = "\n".join(sorted(normalized)) + "\n" if normalized else ""
                (run_dir / f"{source.name}.txt").write_text(txt, encoding="utf-8")

                _log.info("Source %s: %d raw -> %d unique URLs", source.name, len(raw_urls), len(normalized))

                # Track per-source counts for CLI summary (CDX metrics)
                self.per_source_counts[source.name] = (len(raw_urls), len(normalized))

                # Extract pass counts from RegionalSource specifically (D-15)
                if isinstance(source, RegionalSource):
                    pass1_urls = source.pass1_count
                    pass2_urls = source.pass2_count
                    regions_scraped = source.regions_scraped
                    regions_failed = source.regions_failed

        return ScrapeStats(
            pass1_urls=pass1_urls,
            pass2_urls=pass2_urls,
            raw_count=total_raw,
            unique_count=total_unique,
            regions_scraped=regions_scraped,
            regions_failed=regions_failed,
        )


__all__ = [
    "CommonCrawlSource",
    "ForumSource",
    "GitHubSearchSource",
    "GoogleSource",
    "MercusysRegionalSource",
    "ModelSweepSource",
    "RedditSource",
    "ScrapeRunner",
    "ScrapeSource",
    "ScrapeStats",
    "TPLinkGitHubSource",
    "WaybackSource",
    "build_client",
    "url_normalize",
]
