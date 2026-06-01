"""Typer CLI entry point for tpwalk.

Three subcommands:
- scrape: Two-pass regional TP-Link GPL page crawler with Rich progress and summary report.
- verify: Read all discovered URLs, deduplicate, HEAD-check against S3, write output files.
- bruteforce: Active S3 brute-force URL enumeration (BRUT-01, D-09, D-10).

Per CLI-01, CLI-02, SCRP-01, VERF-01, D-07, D-10, D-15. No business logic lives here -- all delegated to runners.
"""

from __future__ import annotations

import asyncio
import os
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from tpwalk.bruteforce import BruteforceRunner
from tpwalk.scrape import ScrapeRunner
from tpwalk.verify import VerifyRunner

app = typer.Typer(
    name="tpwalk",
    help="TP-Link GPL source URL discovery and verification.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_console = Console()


def make_verify_progress() -> Progress:
    """Build the Rich Progress display for the verify HEAD-check loop.

    Column layout per D-07: spinner, description, bar, percentage, M/N count,
    elapsed, ETA, and a live/dead counter updated after each URL completes.

    Live and dead are task-level fields updated by head_check_all after each
    result — they are not standard Rich progress fields and must be declared
    with initial values when the task is created (progress.add_task(..., live=0, dead=0)).

    Returns:
        Configured Progress instance. The caller is responsible for using it as
        a context manager and calling progress.add_task() before starting the run.

    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("[green]{task.fields[live]}[/] live  [red]{task.fields[dead]}[/] dead"),
    )


@app.command("verify")
def cmd_verify(
    concurrency: int = typer.Option(100, "--concurrency", "-c", help="Max concurrent S3 HEAD requests."),
    data_dir: str = typer.Option("data", "--data-dir", help="Root data directory. Reads scrapes/ from here, writes verified output here."),
) -> None:
    """Read all discovered URLs, deduplicate, HEAD-check against S3, write verified output.

    Reads every .txt file in {data_dir}/scrapes/ (seed/ + timestamped run directories),
    normalizes and deduplicates URLs, HEAD-checks each against the S3 origin
    (s3.amazonaws.com/static.tp-link.com) for rich metadata headers, then batch-writes
    verified.json, verified.txt, dead.json, dead.txt.

    Per D-01 through D-07, VERF-01 through VERF-07, CLI-01, CLI-02.
    """
    # Typer is synchronous — must use asyncio.run() to drive async runner (Pitfall 6).
    progress = make_verify_progress()
    runner = VerifyRunner(data_dir=data_dir, concurrency=concurrency)

    with progress:
        task_id = progress.add_task("Verifying URLs…", total=None, live=0, dead=0)
        stats = asyncio.run(runner.run(progress=progress, task_id=task_id))

    # CLI-02 summary report — printed after batch-write completes.
    _console.print()
    _console.print("[bold]Verify complete[/bold]")
    _console.print(f"  URLs found (unique):  {stats.unique_urls}")
    _console.print(f"  Live:                 [green]{stats.live}[/green]")
    _console.print(f"  Dead:                 [red]{stats.dead}[/red]")


def make_scrape_progress() -> Progress:
    """Build the Rich Progress display for the two-pass scrape.

    Two tasks are added at runtime: Pass 1 (regions) and Pass 2 (sub-pages).
    Column layout mirrors make_verify_progress minus the live/dead counter,
    which is not relevant for scraping.

    Per D-10, CLI-01.

    Returns:
        Configured Progress instance. The caller creates two tasks (Pass 1,
        Pass 2) and manages their visibility during the run.

    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    )


@app.command("scrape")
def cmd_scrape(
    data_dir: str = typer.Option("data", "--data-dir", help="Root data directory. Writes scrapes/ subdirectory here."),
    model_sweep: bool = typer.Option(False, "--model-sweep", help="Also run the phppage model-wordlist sweep (heavy: thousands of requests to www.tp-link.com, several minutes)."),  # noqa: FBT001, FBT003
    sweep_max_models: int | None = typer.Option(None, "--sweep-max-models", help="Cap candidate models in --model-sweep (sorted); default no cap."),
) -> None:
    """Discover GPL archive URLs from all sources and write to timestamped run directory.

    Runs the full scrape pipeline:
    - Regional two-pass crawler (productTree JSON + phppage sub-page follow)
    - Wayback CDX API across 5 URL prefixes with resumeKey pagination
    - Common Crawl Index API across all historical indices with page-based pagination

    Results are written as per-source txt files (regional_crawl.txt, wayback_cdx.txt,
    common_crawl.txt) inside a timestamped subdirectory of {data_dir}/scrapes/.

    Per SCRP-01, SCRP-06, SCRP-11, CLI-01, CLI-02, D-10, D-15.
    """
    progress = make_scrape_progress()
    runner = ScrapeRunner(data_dir=data_dir, model_sweep=model_sweep, sweep_max_models=sweep_max_models)

    with progress:
        task_pass1 = progress.add_task("Pass 1: regions...", total=None)
        task_pass2 = progress.add_task("Pass 2: sub-pages...", total=None, visible=False)
        stats = asyncio.run(runner.run(progress=progress, task_pass1=task_pass1, task_pass2=task_pass2))

    # CLI-02 summary report (D-15) -- printed after all sources complete
    _console.print()
    _console.print("[bold]Scrape complete[/bold]")
    _console.print(f"  Regions scraped:      {stats.regions_scraped}")
    _console.print(f"  Regions failed:       [red]{stats.regions_failed}[/red]")
    _console.print(f"  Pass 1 URLs:          {stats.pass1_urls}")
    _console.print(f"  Pass 2 URLs:          {stats.pass2_urls}")
    _console.print(f"  Raw total:            {stats.raw_count}")
    _console.print(f"  Unique (normalized):  [green]{stats.unique_count}[/green]")

    # Per-source summary lines -- all 9 sources (CLI-02, D-15, WR-06).
    # regional_crawl is included here so operators see every source in one summary block.
    # GitHub sources distinguish credential-skip from zero-results (WR-06).
    _source_labels: dict[str, str] = {
        "regional_crawl": "Regional crawl URLs",
        "wayback_cdx": "Wayback CDX URLs",
        "common_crawl": "Common Crawl URLs",
        "github_search": "GitHub search URLs",
        "tplink_github": "TP-Link GitHub URLs",
        "mercusys_regional": "Mercusys URLs",
        "reddit": "Reddit URLs",
        "forums": "Forum URLs",
        "google": "Google URLs",
        "model_sweep": "Model-sweep URLs",
    }
    # Credential check for GitHub sources (WR-06): when GITHUB_TOKEN is absent the GitHub
    # sources short-circuit and return set(), producing (0, 0) in per_source_counts.
    # That is indistinguishable from "ran and found nothing" without the token check here.
    github_token_absent: bool = not os.environ.get("GITHUB_TOKEN")
    _github_source_names: frozenset[str] = frozenset({"github_search", "tplink_github"})

    for source_name, label in _source_labels.items():
        if source_name in runner.per_source_counts:
            raw, unique = runner.per_source_counts[source_name]
            if source_name in _github_source_names and github_token_absent and raw == 0:
                _console.print(f"  {label}:  [yellow]skipped (no GITHUB_TOKEN)[/yellow]")
            else:
                _console.print(f"  {label}:  {unique} unique ({raw} raw)")


class Strategy(StrEnum):
    """Valid values for the --strategy option (D-09, T-05-12 mitigation).

    Typer validates CLI input against this enum automatically; any value outside
    {dates, models, all} raises a BadParameter before the runner is constructed
    (Security Domain V5).
    """

    DATES = "dates"
    MODELS = "models"
    ALL = "all"


def make_bruteforce_progress() -> Progress:
    """Build the Rich Progress display for the brute-force HEAD-check loop.

    Column layout mirrors make_verify_progress but the final TextColumn shows
    a checked/hits counter instead of live/dead (CLI-01).

    checked and hits are task-level fields — they must be registered with initial
    values when the task is created (progress.add_task(..., checked=0, hits=0)).
    Failing to register them before the TextColumn references task.fields[hits]
    raises a KeyError at runtime (RESEARCH Pitfall 4 avoidance).

    Returns:
        Configured Progress instance. Caller must use it as a context manager
        and call progress.add_task("Brute-forcing...", total=None, checked=0, hits=0)
        before starting the run.

    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("[green]{task.fields[hits]}[/] hits  {task.fields[checked]} checked"),
    )


@app.command("bruteforce")
def cmd_bruteforce(  # noqa: PLR0913
    strategy: Strategy = typer.Option(Strategy.ALL, "--strategy", help="Which enumeration(s) to run: dates, models, or all."),  # noqa: B008
    thorough: bool = typer.Option(False, "--thorough", help="Model strategy over ~389 known GPL date dirs (medium coverage, ~40M HEADs)."),  # noqa: FBT001, FBT003
    exhaustive: bool = typer.Option(False, "--exhaustive", help="Model strategy x all date paths — full cross (~203M HEADs). Use --max-candidates as a safety valve."),  # noqa: FBT001, FBT003
    max_candidates: int | None = typer.Option(None, "--max-candidates", help="Hard cap on candidates HEAD-checked (safety valve; no cap by default)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count candidates without issuing any HEAD requests."),  # noqa: FBT001, FBT003
    concurrency: int = typer.Option(100, "--concurrency", "-c", help="Max concurrent S3 HEAD requests."),
    data_dir: str = typer.Option("data", "--data-dir", help="Root data directory; writes scrapes/ here."),
) -> None:
    """Active S3 brute-force URL enumeration — replaces the Phase 5 stub.

    Constructs candidate URLs from two orthogonal generators (date-path and
    model-name), HEAD-checks each against the S3 origin with bounded concurrency,
    live-appends confirmed-live hits to per-strategy txt files in a timestamped
    run directory, and prints a CLI-02 summary.

    Per BRUT-01, BRUT-04, CLI-01, CLI-02, D-09, D-10, D-11.

    Security Domain V5 input validation (T-05-12, T-05-13, T-05-15):
    - --strategy is enum-validated by Typer (bad value → BadParameter before runner runs)
    - --data-dir is checked for parent-traversal (..) and rejected with BadParameter
    - --concurrency must be >= 1 (non-positive value → BadParameter)
    """
    # --- Input validation (Security Domain V5) ---

    # T-05-13: --data-dir path-traversal guard.
    # Reject values containing ".." component(s) so callers cannot escape the
    # intended data root via relative traversal. Check the original string parts
    # (not the resolved absolute path) so "../../etc" is caught before resolution.
    _data_dir_path = Path(data_dir)
    if ".." in _data_dir_path.parts:
        _msg = "--data-dir must not contain parent-traversal ('..')"
        raise typer.BadParameter(_msg, param_hint="'--data-dir'")

    # T-05-15: --concurrency positive-int guard.
    # A semaphore bound of zero would deadlock; negative is nonsensical.
    if concurrency < 1:
        _msg = "--concurrency must be >= 1"
        raise typer.BadParameter(_msg, param_hint="'--concurrency'")

    # T-05-14: --exhaustive without --max-candidates — accept disposition, but warn.
    # The full cross produces ~203M HEADs; users must opt in knowingly.
    if exhaustive and max_candidates is None:
        _console.print("[yellow]WARNING: --exhaustive without --max-candidates will issue ~203M HEAD requests. Use --max-candidates as a safety valve.[/yellow]")

    progress = make_bruteforce_progress()
    runner = BruteforceRunner(
        data_dir=data_dir,
        concurrency=concurrency,
        strategy=strategy.value,
        thorough=thorough,
        exhaustive=exhaustive,
        max_candidates=max_candidates,
        dry_run=dry_run,
    )

    with progress:
        task_id = progress.add_task("Brute-forcing…", total=None, checked=0, hits=0)
        stats = asyncio.run(runner.run(progress=progress, task_id=task_id))

    # CLI-02 summary report (RESEARCH Pattern 5 / PATTERNS section "tpwalk/cli.py").
    _console.print()
    _console.print("[bold]Bruteforce complete[/bold]")
    _console.print(f"  Strategy:             {stats.strategy}")
    _console.print(f"  Tier:                 {stats.tier}")
    _console.print(f"  Candidates checked:   {stats.candidates_checked}")
    _console.print(f"  Hits (date paths):    [green]{stats.hits_dates}[/green]")
    _console.print(f"  Hits (model names):   [green]{stats.hits_models}[/green]")
    _console.print(f"  Transport errors:     [red]{stats.errors}[/red]")
    _console.print(f"  Run dir:              {stats.run_dir}")


def main() -> None:
    """Entry point registered in pyproject.toml as tpwalk = tpwalk.cli:main."""
    app()
