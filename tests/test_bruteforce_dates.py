"""Tests for tpwalk/bruteforce/_dates.py — pure-function determinism and URL format.

No HTTP, no fixtures, no asyncio. Mirrors the discipline of tests/test_normalize.py.
Each test imports the function under test locally to keep the import graph obvious.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def test_iter_date_paths_count_with_bare_year() -> None:
    """3-day range x 2 forms = exactly 6 paths (BRUT-02 count assertion)."""
    from tpwalk.bruteforce._dates import iter_date_paths

    result = list(iter_date_paths(start=date(2023, 1, 1), end=date(2023, 1, 3), include_bare_year=True))
    assert len(result) == 6


def test_iter_date_paths_count_without_bare_year() -> None:
    """3-day range x 1 form = exactly 3 paths (BRUT-02 count assertion, no bare-year)."""
    from tpwalk.bruteforce._dates import iter_date_paths

    result = list(iter_date_paths(start=date(2023, 1, 1), end=date(2023, 1, 3), include_bare_year=False))
    assert len(result) == 3


def test_iter_date_paths_format_and_order() -> None:
    """Single-day range yields exactly the two expected zero-padded path strings in order."""
    from tpwalk.bruteforce._dates import iter_date_paths

    result = list(iter_date_paths(start=date(2023, 1, 1), end=date(2023, 1, 1), include_bare_year=True))
    assert len(result) == 2
    assert result[0] == "/upload/gpl-code/2023/202301/20230101/"
    assert result[1] == "/2023/202301/20230101/"


def test_iter_date_paths_upload_prefix_before_bare_year() -> None:
    """For each day, the /upload/gpl-code/ form is yielded before the bare-year form."""
    from tpwalk.bruteforce._dates import iter_date_paths

    result = list(iter_date_paths(start=date(2023, 6, 15), end=date(2023, 6, 16), include_bare_year=True))
    # day 1: upload first, then bare
    assert result[0] == "/upload/gpl-code/2023/202306/20230615/"
    assert result[1] == "/2023/202306/20230615/"
    # day 2: upload first, then bare
    assert result[2] == "/upload/gpl-code/2023/202306/20230616/"
    assert result[3] == "/2023/202306/20230616/"


def test_iter_date_paths_today_default_does_not_raise() -> None:
    """end=None defaults to date.today(); single-day range yields exactly 2 paths."""
    from tpwalk.bruteforce._dates import iter_date_paths

    today = datetime.now(tz=UTC).date()
    result = list(iter_date_paths(start=today, include_bare_year=True))
    # Single-day range (start == end == today) must yield exactly 2 paths.
    assert len(result) == 2
    expected_upload = f"/upload/gpl-code/{today:%Y}/{today:%Y%m}/{today:%Y%m%d}/"
    expected_bare = f"/{today:%Y}/{today:%Y%m}/{today:%Y%m%d}/"
    assert result[0] == expected_upload
    assert result[1] == expected_bare


def test_iter_date_candidates_url_format_and_variants() -> None:
    """Candidates start with canonical host, contain the base basename, and include _2/_3/_v2/V2 variants."""
    from tpwalk.bruteforce._dates import iter_date_candidates

    basenames = {"AX50v1_GPL.tar.gz"}
    result = list(iter_date_candidates(basenames, start=date(2023, 1, 1), end=date(2023, 1, 1), include_bare_year=False))

    # All URLs must use the canonical https://static.tp-link.com host.
    assert all(u.startswith("https://static.tp-link.com/upload/gpl-code/2023/202301/20230101/") for u in result)

    # The base (unmodified) basename must be present.
    assert "https://static.tp-link.com/upload/gpl-code/2023/202301/20230101/AX50v1_GPL.tar.gz" in result

    # Re-upload variant URLs must be present (D-02/D-03).
    assert "https://static.tp-link.com/upload/gpl-code/2023/202301/20230101/AX50v1_GPL_2.tar.gz" in result
    assert "https://static.tp-link.com/upload/gpl-code/2023/202301/20230101/AX50v1_GPL_3.tar.gz" in result
    assert "https://static.tp-link.com/upload/gpl-code/2023/202301/20230101/AX50v1_GPL_v2.tar.gz" in result
    assert "https://static.tp-link.com/upload/gpl-code/2023/202301/20230101/AX50v1_GPLV2.tar.gz" in result


def test_iter_date_candidates_no_cross_product_with_extensions() -> None:
    """D-01: iter_date_candidates takes basenames, NOT model tokens.

    Total candidate count = date_paths x basenames x variant_count (base + 4 variants = 5).
    No extension multiplication happens inside this module.
    """
    from tpwalk.bruteforce._dates import iter_date_candidates

    basenames = {"AX50v1_GPL.tar.gz", "TL-WR841N_GPL.tar.gz"}
    # 3 days x 1 form (no bare year) = 3 date paths
    result = list(iter_date_candidates(basenames, start=date(2023, 1, 1), end=date(2023, 1, 3), include_bare_year=False))

    # Expected: 3 date_paths x 2 basenames x 5 variants = 30
    assert len(result) == 30
