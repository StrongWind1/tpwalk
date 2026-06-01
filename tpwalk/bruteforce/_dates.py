"""Pure date-path candidate generator for the brute-force date strategy.

Computes candidate S3 URLs from the calendar and a set of known GPL basenames
with no network I/O. The date dimension is ORTHOGONAL to the model dimension
(D-01): this module never multiplies by model patterns or archive extensions.
A given GPL filename lives at exactly one upload date, so multiplying every
basename by every date to discover re-uploaded or moved files is the correct
strategy; multiplying every model pattern by every date (a cross-product) would
produce ~1,799 guaranteed misses per 1,800 guesses.

Two public generators:
- iter_date_paths: yields YYYYMMDD-level S3 path prefixes (two forms per day)
- iter_date_candidates: crosses each path prefix with each known basename and
  its _2/_3/_v2/V2 re-upload variants to yield full canonical https:// URLs

Per BRUT-02 (date-path candidate generation), D-01 (orthogonal, not cross-product),
D-03 (re-published/moved files caught by re-upload variants), D-11 (canonical
https://static.tp-link.com URL form).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# Default start date for the date-path strategy (BRUT-02 SC3; per CONTEXT).
_BASE_DATE = date(2021, 1, 1)

# Known outlier start: one file at /2018/201804/20180404/ (EAP Controller_V2.5_GPL.zip).
# The runner (05-04) decides which date window to pass; this constant is exported
# for runner reference and is not used inside this module's default.
_BARE_YEAR_START = date(2018, 1, 1)

# Archive extensions observed in gpl_urls_master.txt (D-05).
# NOTE: the date strategy consumes full basenames (already have an extension),
# so _EXTENSIONS is exposed here for the model generator (05-03) to import,
# not for use inside iter_date_candidates.
_EXTENSIONS = ("tar.gz", "tar.bz2", "tar", "tgz", "zip", "rar", "gz", "bz2")

# Re-upload variant suffixes inserted before the extension when TP-Link re-publishes
# a file under a new upload date (D-02, D-03).  TP-Link naming patterns observed:
#   ax50v1_GPL_2.tar.gz, ax50v1_GPL_3.tar.gz  (incremental counter)
#   ax50v1_GPL_v2.tar.gz                       (lowercase version token)
#   ax50v1_GPLV2.tar.gz                        (uppercase V2, no separator)
_REUPLOAD_VARIANTS: tuple[str, ...] = ("_2", "_3", "_v2", "V2")


def _insert_variant(basename: str, variant: str) -> str:
    """Return basename with variant inserted before its archive extension.

    For recognized extensions (longest-match first so 'tar.gz' wins over 'gz'),
    the variant token is spliced between the stem and the extension.  Basenames
    with no recognized extension receive the variant appended to the whole name.

    Examples:
        _insert_variant("AX50v1_GPL.tar.gz", "_2")  -> "AX50v1_GPL_2.tar.gz"
        _insert_variant("AX50v1_GPL.tar.gz", "V2")  -> "AX50v1_GPLV2.tar.gz"
        _insert_variant("noext", "_2")               -> "noext_2"

    Args:
        basename: Original filename string (may contain spaces per known corpus).
        variant: Suffix token to insert, e.g. "_2", "_v2", "V2".

    Returns:
        Modified basename string with the variant token incorporated.

    """
    for ext in _EXTENSIONS:
        suffix = "." + ext
        if basename.endswith(suffix):
            stem = basename[: -len(suffix)]
            return f"{stem}{variant}{suffix}"
    # No recognized extension — append variant to the whole basename.
    return basename + variant


def iter_date_paths(
    *,
    start: date = _BASE_DATE,
    end: date | None = None,
    include_bare_year: bool = True,
) -> Iterator[str]:
    """Yield YYYYMMDD-level S3 path prefixes from start to end (inclusive).

    Two path forms are yielded for each calendar day (if include_bare_year is
    True):
      /upload/gpl-code/YYYY/YYYYMM/YYYYMMDD/   (2022+ date-hierarchical convention)
      /YYYY/YYYYMM/YYYYMMDD/                   (bare-year; one known 2018 outlier)

    The /upload/gpl-code/ form is always yielded first for each day.  When
    include_bare_year is False, only the /upload/gpl-code/ form is yielded.

    If end is None, it defaults to date.today() so the candidate set grows by
    one day per calendar day without a hardcoded upper bound (Pitfall 2 from
    RESEARCH: never hardcode a date count).

    All YYYYMM and YYYYMMDD components are zero-padded by strftime semantics.

    Args:
        start: First date to yield paths for.  Defaults to _BASE_DATE (2021-01-01).
        end: Last date to yield paths for (inclusive).  None means today.
        include_bare_year: Whether to also yield the /YYYY/YYYYMM/YYYYMMDD/ form.

    Yields:
        Path prefix strings starting and ending with '/'.

    Per BRUT-02, D-11.

    """
    if end is None:
        end = datetime.now(tz=UTC).date()
    current = start
    while current <= end:
        y = current.strftime("%Y")
        ym = current.strftime("%Y%m")
        ymd = current.strftime("%Y%m%d")
        yield f"/upload/gpl-code/{y}/{ym}/{ymd}/"
        if include_bare_year:
            yield f"/{y}/{ym}/{ymd}/"
        current += timedelta(days=1)


def iter_date_candidates(
    basenames: set[str],
    *,
    start: date = _BASE_DATE,
    end: date | None = None,
    include_bare_year: bool = True,
) -> Iterator[str]:
    """Yield canonical https://static.tp-link.com URLs for each date path x basename x variant.

    For every path prefix from iter_date_paths, and for every basename in the
    provided set, yields:
      1. The base URL (path + unmodified basename)
      2. Four re-upload variant URLs (path + variant-modified basename) per D-02/D-03

    Re-upload variants are produced by _insert_variant with each token from
    _REUPLOAD_VARIANTS: "_2", "_3", "_v2", "V2".

    ORTHOGONALITY (D-01): basenames is a set of full filenames sourced from
    gpl_urls_master.txt, NOT a set of model name tokens.  This module never
    multiplies by model patterns or archive extensions — that would produce a
    cross-product forbidden by D-01.  The model generator (05-03) handles
    model x extension enumeration on the flat /resources/gpl/ prefix.

    All generated URLs use https://static.tp-link.com as the host (D-11),
    matching the canonical form produced by url_normalize and present in
    gpl_urls_master.txt.

    Args:
        basenames: Set of full GPL archive filenames (e.g. "AX50v1_GPL.tar.gz").
        start: First date to generate candidates for.
        end: Last date (inclusive).  None means today.
        include_bare_year: Whether to include the /YYYY/YYYYMM/YYYYMMDD/ path form.

    Yields:
        Full canonical https://static.tp-link.com URLs, one per (path, basename, variant).

    Per BRUT-02, D-01, D-02, D-03, D-11.

    """
    for path in iter_date_paths(start=start, end=end, include_bare_year=include_bare_year):
        for basename in basenames:
            yield f"https://static.tp-link.com{path}{basename}"
            for variant in _REUPLOAD_VARIANTS:
                yield f"https://static.tp-link.com{path}{_insert_variant(basename, variant)}"
