"""BruteforceRunner orchestrator for active S3 candidate enumeration.

This module delivers the active-enumeration slice of tpwalk: it feeds candidates
from two orthogonal generators (date-path from _dates.py, model-name from _models.py)
through a Semaphore-bounded asyncio.TaskGroup of exists_url() calls, and live-appends
each confirmed-live (200) URL to its per-strategy txt file the instant the HEAD
returns True (D-08). 404 misses (>99% of candidates) are silently discarded.

Key design decisions:
- D-01: The two generators are strictly orthogonal -- never cross-product.
  The date strategy crosses known GPL basenames x date paths.
  The model strategy uses flat /resources/gpl/ (or known/all date paths for tiers).
- D-07: Hits go to split files: bruteforce_dates.txt + bruteforce_models.txt.
- D-08: Live-append via asyncio.Lock per output file (RESEARCH Pattern 4).
  The lock guards only the open+write call, never the HEAD (Pitfall 3).
- D-10: BruteforceRunner is a standalone orchestrator, NOT a ScrapeSource.
- D-11: Run-dir format is data/scrapes/{YYYY-MM-DDThhmm}/ -- same as ScrapeRunner.
  DIR-03 requirement text says "YYYY-MM-DD" but D-11 locks the timestamp form
  to match read_all_txt rglob patterns from Phase 1 (Pitfall 5).

Requirement coverage: BRUT-01, BRUT-04, DIR-03, D-01, D-02, D-07, D-08, D-10, D-11.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from tpwalk._client import build_client
from tpwalk._normalize import url_normalize
from tpwalk.bruteforce._dates import _BASE_DATE, iter_date_candidates, iter_date_paths
from tpwalk.bruteforce._head import exists_url
from tpwalk.bruteforce._models import _resolve_ref_file, extract_firmware_models, iter_model_candidates, load_firmware_keys
from tpwalk.models import BruteforceStats

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx
    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# Per-strategy output filenames (D-07).
_OUTPUT_DATES = "bruteforce_dates.txt"
_OUTPUT_MODELS = "bruteforce_models.txt"

# Regex to extract the YYYY/YYYYMM/YYYYMMDD/ segment from a URL path.
# Matches both /upload/gpl-code/YYYY/YYYYMM/YYYYMMDD/ and bare /YYYY/YYYYMM/YYYYMMDD/ forms.
_DATE_PATH_RE = re.compile(
    r"(?:upload/gpl-code/|/)(\d{4}/\d{6}/\d{8}/)",
)


class BruteforceRunner:
    """Orchestrates date-path and model-name HEAD enumeration.

    Creates a timestamped run directory, assembles candidates from two orthogonal
    generators per strategy/tier, HEAD-checks them via exists_url() under a shared
    Semaphore inside a TaskGroup, and live-appends each 200 hit to its per-strategy
    file under a per-file asyncio.Lock.

    Does NOT collect a set and batch-write at the end -- each hit is written the
    instant it arrives (D-08 KEY INVERSION from ScrapeRunner's batch-write model).
    404 misses are silently discarded; never written.

    Per BRUT-01, BRUT-04, D-02, D-09, D-10, DIR-03.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        data_dir: str = "data",
        firmware_listing: str = "ref_gpl_data/firmware_s3_listing.json",
        concurrency: int = 100,
        strategy: str = "all",
        thorough: bool = False,
        exhaustive: bool = False,
        max_candidates: int | None = None,
        dry_run: bool = False,
    ) -> None:
        """Configure the brute-force runner.

        All parameters are keyword-only (project conventions: keyword-only for 3+ params).

        Tier precedence: exhaustive wins if both thorough and exhaustive are True.
        Tiers per D-02:
          - default:    model strategy on flat /resources/gpl/; date strategy uses
                        known basenames x all date paths from _BASE_DATE to today.
          - thorough:   model strategy over the ~389 known GPL date dirs extracted
                        from gpl_urls_master.txt (RESEARCH Open Q3: exact set).
          - exhaustive: model strategy x all date paths (full cross; ~203M HEADs).

        Args:
            data_dir: Root data directory; scrapes/ subdirectory is created here.
            firmware_listing: Path to firmware_s3_listing.json (D-06).
            concurrency: Max concurrent S3 HEAD requests (bounded by Semaphore).
            strategy: Which generator(s) to run: "dates", "models", or "all".
            thorough: If True, model strategy uses ~389 known GPL date dirs.
            exhaustive: If True, model strategy uses all date paths (full cross, D-01 opt-in).
            max_candidates: Hard cap on total candidates (safety valve; None = no cap).
            dry_run: If True, enumerate and count candidates but issue ZERO HEADs.

        """
        self._data_dir = Path(data_dir)
        self._firmware_listing = firmware_listing
        self._concurrency = concurrency
        self._strategy = strategy
        self._max_candidates = max_candidates
        self._dry_run = dry_run
        # exhaustive wins if both flags are set (D-02 tier precedence)
        self._tier = "exhaustive" if exhaustive else ("thorough" if thorough else "default")

    def _load_known_basenames_and_date_dirs(self) -> tuple[set[str], list[str]]:
        """Extract known GPL basenames and known date-path prefixes from gpl_urls_master.txt.

        Reads ref_gpl_data/gpl_urls_master.txt (one full HTTPS URL per line) and:
        1. Extracts the basename (last path segment) of each URL into a set.
        2. Parses any /upload/gpl-code/YYYY/YYYYMM/YYYYMMDD/ or
           /YYYY/YYYYMM/YYYYMMDD/ segment into the known-date-dirs set,
           then formats each as /upload/gpl-code/{YYYY}/{YYYYMM}/{YYYYMMDD}/
           to match the iter_date_paths format (D-11).

        The known-date-dirs set is the "exactly 389 known GPL date dirs" referenced
        in RESEARCH Open Q3 -- used by the thorough tier for the model strategy.
        The basenames set seeds the default-tier date strategy (D-02).

        If the file is missing, logs a WARNING and returns empty sets (graceful
        recall degradation -- the run proceeds but with no known basenames).

        Returns:
            Tuple of (basenames: set[str], known_date_dirs: list[str]).
            known_date_dirs is sorted for deterministic output.

        """
        # Resolve the corpus path -- try relative to cwd first, then project root heuristic
        corpus_candidates = [
            Path("ref_gpl_data/gpl_urls_master.txt"),
            Path(__file__).parent.parent.parent / "ref_gpl_data" / "gpl_urls_master.txt",
        ]
        corpus_path: Path | None = None
        for candidate in corpus_candidates:
            if candidate.exists():
                corpus_path = candidate
                break

        if corpus_path is None:
            _log.warning("gpl_urls_master.txt not found -- known basenames and date dirs unavailable; date strategy may produce zero candidates (recall degraded).")
            return set(), []

        basenames: set[str] = set()
        date_dirs: set[str] = set()

        for raw_line in corpus_path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            parts = urlsplit(stripped)
            # Extract basename (last path segment after final '/') and decode
            # percent-encoding so date and model strategies canonicalize basenames
            # identically (IN-02: both strategies now unquote; model strategy already
            # did this via load_known_basenames).
            basename = unquote(parts.path.rpartition("/")[2])
            if basename:
                basenames.add(basename)
            # Extract the YYYYMMDD path segment if present in the URL path
            m = _DATE_PATH_RE.search(parts.path)
            if m:
                seg = m.group(1)  # e.g. "2024/202401/20240115/"
                seg_parts = seg.rstrip("/").split("/")
                if len(seg_parts) == 3:  # noqa: PLR2004
                    y, ym, ymd = seg_parts
                    date_dirs.add(f"/upload/gpl-code/{y}/{ym}/{ymd}/")

        _log.debug(
            "Loaded %d known basenames and %d known date dirs from %s",
            len(basenames),
            len(date_dirs),
            corpus_path,
        )
        return basenames, sorted(date_dirs)

    def _iter_candidates(
        self,
        *,
        known_basenames: set[str],
        known_date_dirs: list[str],
        models: set[str],
    ) -> Iterator[tuple[str, str]]:
        """Yield (strategy_name, url) pairs for the configured strategy and tier.

        strategy_name is "dates" or "models" -- used by run() to route hits to
        the correct per-strategy output file.

        D-01: The two strategies are concatenated, NOT nested. A date-path
        candidate is never crossed with a model-pattern candidate. The model
        strategy generates candidates independently of the date strategy.

        D-02 tier behavior:
          - default:    date strategy = known_basenames x all date paths from _BASE_DATE.
                        model strategy = iter_model_candidates(models, date_paths=None)
                        [flat /resources/gpl/ prefix].
          - thorough:   model strategy = iter_model_candidates(models, date_paths=known_date_dirs)
                        [~389 known GPL date dirs from gpl_urls_master.txt].
          - exhaustive: model strategy = iter_model_candidates(models, date_paths=<all date paths>)
                        [full cross; this is the ONLY cross-product; opt-in only].

        max_candidates is applied as a hard cap on the total yielded count.

        Args:
            known_basenames: Set of GPL basenames from gpl_urls_master.txt (date strategy).
            known_date_dirs: List of known /upload/gpl-code/YYYY/YYYYMM/YYYYMMDD/ prefixes (thorough).
            models: Set of model name tokens from firmware_s3_listing.json (model strategy).

        Yields:
            Tuples of (strategy_name, url) where strategy_name is "dates" or "models".

        """
        count = 0

        # --- Date strategy (D-01: orthogonal to model strategy) ---
        if self._strategy in {"dates", "all"}:
            for url in iter_date_candidates(
                known_basenames,
                start=_BASE_DATE,
                end=None,
                include_bare_year=True,
            ):
                if self._max_candidates is not None and count >= self._max_candidates:
                    return
                yield "dates", url
                count += 1

        # --- Model strategy (D-01: orthogonal to date strategy; tiers per D-02) ---
        if self._strategy in {"models", "all"}:
            if self._tier == "exhaustive":
                # Full cross: model x all date paths (opt-in only; ~203M candidates)
                all_date_paths = list(iter_date_paths(start=_BASE_DATE, end=None, include_bare_year=True))
                model_date_paths: list[str] | None = all_date_paths
            elif self._tier == "thorough":
                # Known date dirs only (~389 known GPL date dirs; RESEARCH Open Q3)
                model_date_paths = known_date_dirs or None
            else:
                # Default: flat /resources/gpl/ prefix (no date dimension for models)
                model_date_paths = None

            for url in iter_model_candidates(models, date_paths=model_date_paths):
                if self._max_candidates is not None and count >= self._max_candidates:
                    return
                yield "models", url
                count += 1

    async def _append_hit(self, *, url: str, path: Path, lock: asyncio.Lock, errors: list[int]) -> None:
        """Append a confirmed-live URL to the strategy output file.

        Acquires the per-file lock before opening and writing so concurrent
        TaskGroup workers never interleave writes. File is opened per-call
        in append mode for flush-on-close safety on interruption (D-08, RESEARCH Pattern 4).

        The lock guards ONLY the open+write call -- never the HEAD request (Pitfall 3).

        A transient write failure (ENOSPC, EACCES, FS error) is caught, logged, and
        counted in the errors counter (WR-01) so a single disk event does not abort a
        multi-hour sweep via an uncaught ExceptionGroup.

        Args:
            url: Canonical (url_normalize'd) HTTPS URL to append.
            path: Path to the per-strategy output file.
            lock: Per-file asyncio.Lock serializing concurrent appends (T-05-11 mitigation).
            errors: Mutable single-element list used as a shared counter; incremented on write failure.

        Per D-08, BRUT-04, RESEARCH Pattern 4.

        """
        try:
            async with lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(url + "\n")
        except OSError:
            _log.exception("Failed to append hit %s to %s", url, path)
            errors[0] += 1

    async def run(  # noqa: C901, PLR0915
        self,
        *,
        progress: Progress | None = None,
        task_id: TaskID | None = None,
    ) -> BruteforceStats:
        """Execute the brute-force enumeration and return summary statistics.

        Pipeline:
        1. Create timestamped run directory (DIR-03, D-11 format YYYY-MM-DDThhmm).
        2. Pre-create both output files (stable targets for verify rglob / CLI summary).
        3. Load known basenames + date dirs from gpl_urls_master.txt.
        4. Load model tokens from firmware_s3_listing.json.
        5. If dry_run: iterate candidates counting only (zero HEADs); return stats.
        6. Otherwise: build Semaphore + asyncio.TaskGroup; for each candidate
           create a _check task; on hit live-append to the correct file under lock.
        7. Return BruteforceStats with all counters populated.

        errors counts output-file write failures from _append_hit (WR-01): ENOSPC,
        EACCES, or other FS errors that prevented a confirmed hit from being persisted.
        Transient transport failures inside exists_url() are still folded into misses
        (False return) per RESEARCH Pattern 1 / D-10. The errors field is reported in
        the CLI-02 summary so operators can see when disk events caused hits to be lost.

        Args:
            progress: Rich Progress instance for live display, or None.
            task_id: Rich task ID for the progress bar task, or None.

        Returns:
            BruteforceStats with candidates_checked, hits_dates, hits_models,
            errors, strategy, tier, dry_run, and run_dir populated.

        Per BRUT-04, DIR-03, D-08, D-10, D-11.

        """
        # DIR-03 says "YYYY-MM-DD" in requirement text but D-11 locks the
        # implementation to YYYY-MM-DDThhmm -- same format as ScrapeRunner --
        # so read_all_txt rglob and multiple same-day runs work correctly (Pitfall 5).
        stamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H%M")
        run_dir = self._data_dir / "scrapes" / stamp
        run_dir.mkdir(parents=True, exist_ok=True)

        # Pre-create both output files so verify rglob and CLI summary have
        # stable targets even when a strategy produces zero hits (D-07).
        dates_path = run_dir / _OUTPUT_DATES
        models_path = run_dir / _OUTPUT_MODELS
        dates_path.touch()
        models_path.touch()

        _log.info(
            "BruteforceRunner: run dir %s strategy=%s tier=%s dry_run=%s",
            run_dir,
            self._strategy,
            self._tier,
            self._dry_run,
        )

        # Per-file locks -- one per output file so dates and models don't block each other (D-08).
        dates_lock = asyncio.Lock()
        models_lock = asyncio.Lock()

        # --- Load reference data ---
        known_basenames, known_date_dirs = self._load_known_basenames_and_date_dirs()
        try:
            # Resolve the firmware listing path: try as given first, then __file__-relative
            # so an installed console-script user doesn't need to be in the project root (WR-02).
            firmware_path = Path(self._firmware_listing)
            if not firmware_path.exists():  # noqa: ASYNC240 -- sync FS check in async context is intentional (httpx-based, not trio/anyio)
                resolved = _resolve_ref_file(firmware_path.name)
                if resolved is not None:
                    firmware_path = resolved
            fw_keys = load_firmware_keys(listing_path=firmware_path)
            models = extract_firmware_models(fw_keys)
        except OSError, ValueError:
            _log.warning(
                "firmware_s3_listing.json not found or malformed at %s -- model strategy will produce zero candidates.",
                self._firmware_listing,
            )
            models = set()

        # --- Dry-run: count only, no HEADs ---
        if self._dry_run:
            candidates_counted = 0
            for _strategy_name, _url in self._iter_candidates(
                known_basenames=known_basenames,
                known_date_dirs=known_date_dirs,
                models=models,
            ):
                candidates_counted += 1
            _log.info("BruteforceRunner dry_run: %d candidates enumerated, 0 HEADs issued", candidates_counted)
            return BruteforceStats(
                candidates_checked=0,
                hits_dates=0,
                hits_models=0,
                errors=0,
                strategy=self._strategy,
                tier=self._tier,
                dry_run=True,
                run_dir=str(run_dir),
            )

        # --- Live run ---
        sem = asyncio.Semaphore(self._concurrency)

        # Mutable counters shared across inner coroutine via closure.
        checked: list[int] = [0]
        hits_dates: list[int] = [0]
        hits_models: list[int] = [0]
        # errors counts write failures from _append_hit (WR-01).
        # Transport failures from exists_url() are still folded into False misses (RESEARCH Pattern 1).
        errors: list[int] = [0]

        async def _check(strategy_name: str, url: str, client: httpx.AsyncClient) -> None:
            """Check one candidate URL and live-append if it is a hit.

            exists_url() acquires the Semaphore internally so it never holds
            the lock during the HEAD (Pitfall 3 avoidance -- lock is acquired
            only inside _append_hit for the open+write critical section).

            Args:
                strategy_name: "dates" or "models" -- routes the hit to the correct file.
                url: Canonical HTTPS candidate URL.
                client: Shared AsyncClient.

            """
            hit = await exists_url(url=url, client=client, sem=sem)
            checked[0] += 1

            if hit:
                normalized = url_normalize(url)
                if strategy_name == "dates":
                    await self._append_hit(url=normalized, path=dates_path, lock=dates_lock, errors=errors)
                    hits_dates[0] += 1
                else:
                    await self._append_hit(url=normalized, path=models_path, lock=models_lock, errors=errors)
                    hits_models[0] += 1

            if progress is not None and task_id is not None:
                progress.update(
                    task_id,
                    advance=1,
                    checked=checked[0],
                    hits=hits_dates[0] + hits_models[0],
                )

        # Fixed-pool worker pattern: creates exactly self._concurrency workers, each
        # pulling from the shared sync generator until it is exhausted.  The sync
        # generator is safe to share across cooperatively-scheduled coroutines because
        # _iter_candidates contains no await points, so Python's cooperative scheduler
        # never interleaves two calls to next() on the same generator object.
        # This caps live Tasks at concurrency (CR-01: prevents ~24.9M Task OOM).
        candidates = self._iter_candidates(
            known_basenames=known_basenames,
            known_date_dirs=known_date_dirs,
            models=models,
        )

        async def _worker(client: httpx.AsyncClient) -> None:
            """Pull candidates from the shared iterator and HEAD-check each one.

            Runs until the shared iterator is exhausted.  Each call to _check
            acquires the Semaphore internally, so per-HEAD concurrency is unchanged.

            Args:
                client: Shared AsyncClient passed in from the outer scope.

            """
            for strategy_name, url in candidates:
                await _check(strategy_name, url, client)

        async with build_client() as client, asyncio.TaskGroup() as tg:
            for _ in range(self._concurrency):
                tg.create_task(_worker(client))

        _log.info(
            "BruteforceRunner complete: checked=%d hits_dates=%d hits_models=%d errors=%d",
            checked[0],
            hits_dates[0],
            hits_models[0],
            errors[0],
        )

        return BruteforceStats(
            candidates_checked=checked[0],
            hits_dates=hits_dates[0],
            hits_models=hits_models[0],
            errors=errors[0],
            strategy=self._strategy,
            tier=self._tier,
            dry_run=False,
            run_dir=str(run_dir),
        )


__all__ = ["BruteforceRunner"]
