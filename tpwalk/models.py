"""Typed data models for tpwalk verify pipeline output.

Three frozen dataclasses represent the core pipeline types:
- VerifiedEntry: a confirmed-live archive with S3 origin metadata (D-06)
- DeadEntry: a dead or unreachable URL with error classification (D-04)
- RunStats: summary counts from a single verify run (CLI-02)

These are the canonical output types shared by the verify pipeline, CLI output
layer, and any future pipeline stages.

Schema per D-06. All x-amz-* derived fields are str | None because:
- Legacy files (pre-2022, /resources/gpl/) return x-amz-version-id: null (the
  string "null") and omit replication-status and encryption headers entirely.
- Modern files (2022+, /upload/gpl-code/) return all three populated.
The caller must normalize the "null" string to Python None before constructing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerifiedEntry:
    """One confirmed-live GPL archive URL with S3 origin metadata.

    All optional fields correspond to x-amz-* headers that are only
    present on files uploaded after 2022. Legacy files (pre-2022,
    /resources/gpl/) return null or absent values — these must be
    typed str | None, not str.

    Per D-06. Field order matches the D-06 schema definition.
    """

    url: str
    size: int
    etag: str
    content_type: str
    last_modified: str
    version_id: str | None  # "null" string from S3 normalized to None by caller
    replication_status: str | None
    encryption: str | None
    server: str
    status: int
    checked_at: str  # ISO 8601, UTC


@dataclass(frozen=True)
class DeadEntry:
    """One URL that returned 404/403 or a network error during HEAD-check.

    error_type is one of:
    - "http": received a non-200 HTTP response (status code is set)
    - "timeout": request timed out before any response (status is None)
    - "network": connection-level failure (status is None)

    Per D-04.
    """

    url: str
    status: int | None  # None for network/timeout errors
    error_type: str
    checked_at: str  # ISO 8601, UTC


@dataclass(frozen=True)
class RunStats:
    """Summary counts from a single VerifyRunner.run() execution.

    Provides the totals needed for the CLI-02 summary report printed after
    each verify run. The invariant live + dead == unique_urls holds: every
    unique URL is either confirmed live or dead after the HEAD-check pass.

    Per CLI-02.
    """

    total_urls: int  # Raw line count before deduplication
    unique_urls: int  # After url_normalize dedup
    live: int  # 200 responses
    dead: int  # 404/403/timeout/network errors


@dataclass(frozen=True)
class ScrapeStats:
    """Summary counts from a ScrapeRunner.run() execution for the CLI-02 summary report.

    pass1_urls: URLs extracted from direct productTree href links (Pass 1)
    pass2_urls: URLs extracted from phppage sub-page follow (Pass 2)
    raw_count: Total URLs before normalization/dedup (pass1 + pass2 summed, may include dups)
    unique_count: After url_normalize() deduplication within the source
    regions_scraped: Total regions successfully scraped (including those with 0 URLs)
    regions_failed: Regions skipped due to HTTP errors or parse failures

    Per D-15, CLI-02.
    """

    pass1_urls: int
    pass2_urls: int
    raw_count: int
    unique_count: int
    regions_scraped: int
    regions_failed: int


@dataclass(frozen=True)
class BruteforceStats:
    """Summary counts from a BruteforceRunner.run() execution for the CLI-02 summary report.

    Brute-force analog of ScrapeStats (D-07). When dry_run=True, candidates are
    enumerated but no HEAD requests are issued, so candidates_checked == 0 and
    both hit counts == 0.

    candidates_checked: Total HEAD requests issued (0 if dry_run=True)
    hits_dates: Confirmed-live URLs discovered by the date-path strategy
    hits_models: Confirmed-live URLs discovered by the model-name strategy
    errors: Transport errors (timeout/network) — not 404 misses
    strategy: One of "dates" | "models" | "all"
    tier: One of "default" | "thorough" | "exhaustive"
    dry_run: True if --dry-run was passed (no HEADs issued, hit counts both 0)
    run_dir: Timestamped run directory path as a string for the CLI-02 summary

    Per CLI-02, D-07.
    """

    candidates_checked: int  # total HEADs issued; 0 if dry_run=True
    hits_dates: int  # confirmed-live URLs from date-path strategy
    hits_models: int  # confirmed-live URLs from model-name strategy
    errors: int  # transport errors (timeout/network); not 404 misses
    strategy: str  # "dates" | "models" | "all"
    tier: str  # "default" | "thorough" | "exhaustive"
    dry_run: bool  # if True, candidates enumerated but no HEADs issued
    run_dir: str  # timestamped run directory path for CLI-02 summary
