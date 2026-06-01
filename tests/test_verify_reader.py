"""Tests for tpwalk.verify._reader — file discovery, normalization, and dedup.

Tests verify that read_all_txt correctly finds txt files in seed/ and timestamped
subdirectories, normalizes URLs, deduplicates, skips blank lines, and never
writes to the seed directory.

Per VERF-02, VERF-03, DIR-01, DIR-04.
"""

from __future__ import annotations

from pathlib import Path


def test_read_all_txt_finds_seed_files(scrapes_dir: Path) -> None:
    """URLs in seed/*.txt are returned by read_all_txt."""
    from tpwalk.verify._reader import read_all_txt

    seed = scrapes_dir / "seed"
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz\n",
        encoding="utf-8",
    )
    result = read_all_txt(scrapes_dir)
    assert "https://static.tp-link.com/resources/gpl/11N_GPL.tgz" in result


def test_read_all_txt_finds_timestamped_dirs(scrapes_dir: Path) -> None:
    """URLs in timestamped run directories are found via recursive glob."""
    from tpwalk.verify._reader import read_all_txt

    ts_dir = scrapes_dir / "2026-05-27T1430"
    ts_dir.mkdir(parents=True)
    (ts_dir / "regional.txt").write_text(
        "https://static.tp-link.com/upload/gpl-code/2026/202605/20260527/GPL_XYZ.tar.gz\n",
        encoding="utf-8",
    )
    result = read_all_txt(scrapes_dir)
    assert "https://static.tp-link.com/upload/gpl-code/2026/202605/20260527/GPL_XYZ.tar.gz" in result


def test_read_all_txt_combines_all_subdirs(scrapes_dir: Path) -> None:
    """URLs from seed/ and timestamped dirs are unioned into one set."""
    from tpwalk.verify._reader import read_all_txt

    seed = scrapes_dir / "seed"
    (seed / "a.txt").write_text(
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz\n",
        encoding="utf-8",
    )
    ts_dir = scrapes_dir / "2026-05-28T0900"
    ts_dir.mkdir(parents=True)
    (ts_dir / "b.txt").write_text(
        "https://static.tp-link.com/resources/gpl/TP-LINK_GPL_1201.tar.gz\n",
        encoding="utf-8",
    )
    result = read_all_txt(scrapes_dir)
    assert "https://static.tp-link.com/resources/gpl/11N_GPL.tgz" in result
    assert "https://static.tp-link.com/resources/gpl/TP-LINK_GPL_1201.tar.gz" in result
    assert len(result) == 2


def test_read_all_txt_normalizes_urls(scrapes_dir: Path) -> None:
    """A raw-space URL and a percent-encoded URL for the same file collapse to one entry."""
    from tpwalk.verify._reader import read_all_txt

    seed = scrapes_dir / "seed"
    raw_space = "https://static.tp-link.com/2018/201804/20180404/EAP Controller_V2.5_GPL.zip"
    pct_encoded = "https://static.tp-link.com/2018/201804/20180404/EAP%20Controller_V2.5_GPL.zip"
    (seed / "urls.txt").write_text(
        f"{raw_space}\n{pct_encoded}\n",
        encoding="utf-8",
    )
    result = read_all_txt(scrapes_dir)
    # Both forms collapse to one canonical URL
    assert len(result) == 1


def test_read_all_txt_skips_blank_lines(scrapes_dir: Path) -> None:
    """Blank lines in txt files are silently ignored."""
    from tpwalk.verify._reader import read_all_txt

    seed = scrapes_dir / "seed"
    (seed / "urls.txt").write_text(
        "\nhttps://static.tp-link.com/resources/gpl/11N_GPL.tgz\n\n\n",
        encoding="utf-8",
    )
    result = read_all_txt(scrapes_dir)
    assert len(result) == 1
    assert "https://static.tp-link.com/resources/gpl/11N_GPL.tgz" in result


def test_read_all_txt_empty_dir(scrapes_dir: Path) -> None:
    """Empty scrapes_dir (no txt files) returns an empty set."""
    from tpwalk.verify._reader import read_all_txt

    result = read_all_txt(scrapes_dir)
    assert result == set()


def test_reads_bruteforce_files(scrapes_dir: Path) -> None:
    """DIR-04 regression: read_all_txt picks up bruteforce_dates.txt and bruteforce_models.txt.

    Proves that the existing rglob("*.txt") in read_all_txt covers per-strategy
    brute-force output files in a timestamped run directory with no change to the
    reader. Both date-path URLs (bruteforce_dates.txt) and flat /resources/gpl/
    URLs (bruteforce_models.txt) are included in the union result.

    No change to tpwalk/verify/_reader.py — the rglob contract is locked here (DIR-04).
    """
    from tpwalk.verify._reader import read_all_txt

    run_dir = scrapes_dir / "2026-05-29T1500"
    run_dir.mkdir(parents=True)

    # Date-path URL written by the date strategy (D-07, bruteforce_dates.txt)
    date_url = "https://static.tp-link.com/upload/gpl-code/2026/202605/20260526/AX50v1_GPL.tar.gz"
    (run_dir / "bruteforce_dates.txt").write_text(f"{date_url}\n", encoding="utf-8")

    # Flat /resources/gpl/ URL written by the model strategy (D-07, bruteforce_models.txt)
    model_url = "https://static.tp-link.com/resources/gpl/GPL_TL-WR841N.tar.gz"
    (run_dir / "bruteforce_models.txt").write_text(f"{model_url}\n", encoding="utf-8")

    result = read_all_txt(scrapes_dir)

    assert date_url in result, f"bruteforce_dates.txt URL not found in read_all_txt result; got: {result}"
    assert model_url in result, f"bruteforce_models.txt URL not found in read_all_txt result; got: {result}"


def test_is_gpl_archive_url_keeps_real_gpl() -> None:
    """Every GPL archive shape is kept, including legacy .bz / .tgz / .7z and mercusys."""
    from tpwalk.verify._reader import is_gpl_archive_url

    keep = [
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz",
        "https://static.tp-link.com/upload/gpl-code/2026/202603/20260306/OC300 1.20.7z",
        "https://static.tp-link.com/upload/gpl-code/2022/202209/20220920/e4sv3-gpl.tar.bz",
        "https://static.tp-link.com/upload/gpl-code/2024/202410/20241018/Tapo robot vaccum.gz",  # 'gpl' via gpl-code dir
        "https://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz",
        "https://static.tp-link.com/2018/201804/20180404/EAP Controller_V2.5_GPL.zip",
    ]
    for url in keep:
        assert is_gpl_archive_url(url), f"GPL archive wrongly dropped: {url}"


def test_is_gpl_archive_url_drops_non_gpl_assets() -> None:
    """Broad Common Crawl noise — docs, manuals, images, software — is dropped."""
    from tpwalk.verify._reader import is_gpl_archive_url

    drop = [
        "https://static.tp-link.com/resources/document/datasheet.pdf",
        "https://static.tp-link.com/res/down/doc/TL-WR940N_V1_UG.pdf",
        "https://static.tp-link.com/upload/manual/2025/202511/20251124/manual.pdf",
        "https://static.tp-link.com/upload/product-overview/2026/202603/20260306/hero.jpg",
        "https://static.tp-link.com/resources/software/Archer_setup.zip",  # software zip, no 'gpl'
        "https://static.tp-link.com/images/product.png",
    ]
    for url in drop:
        assert not is_gpl_archive_url(url), f"non-GPL asset wrongly kept: {url}"


def test_seed_readonly(scrapes_dir: Path) -> None:
    """read_all_txt does not create or modify any files in seed/."""
    from tpwalk.verify._reader import read_all_txt

    seed = scrapes_dir / "seed"
    (seed / "urls.txt").write_text(
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz\n",
        encoding="utf-8",
    )

    # Record state before the call
    before_files = {str(p) for p in seed.rglob("*")}
    before_mtime = (seed / "urls.txt").stat().st_mtime

    read_all_txt(scrapes_dir)

    # Verify nothing changed
    after_files = {str(p) for p in seed.rglob("*")}
    after_mtime = (seed / "urls.txt").stat().st_mtime

    assert before_files == after_files, "seed/ directory contents changed"
    assert before_mtime == after_mtime, "seed/ file was modified"
