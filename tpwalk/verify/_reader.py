"""File reader for the tpwalk verify pipeline.

Discovers and reads all .txt files in the scrapes directory tree (seed/ and all
timestamped run directories), normalizes each URL, and returns a deduplicated set.

Per VERF-02, VERF-03, DIR-04.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tpwalk._normalize import url_normalize

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)

# Archive extensions that mark a URL as a GPL source candidate. Derived from the
# full known GPL corpus, which uses .gz/.bz2/.zip/.tar/.rar/.tgz/.7z and the
# legacy .bz — so the list must stay exhaustive or real tarballs get gated out.
_GPL_ARCHIVE_EXTS: tuple[str, ...] = (".tar.gz", ".tgz", ".tar.bz2", ".tar.bz", ".tar.xz", ".tar", ".zip", ".rar", ".gz", ".bz2", ".bz", ".7z", ".xz")


def is_gpl_archive_url(url: str) -> bool:
    """Return True if a normalized URL is a GPL source archive, not a doc/image.

    Deterministic gate (no heuristic guessing): the path must contain "gpl" and
    the URL must end in a known archive extension. Every real GPL key satisfies
    both — they live under /upload/gpl-code/, /resources/gpl/, or mercusys /gpl/
    (or carry _GPL_ in the filename) and end in an archive suffix. The Common
    Crawl source queries a broad wildcard (static.tp-link.com/*) and captures
    every crawled asset — manuals, datasheets, product images, software. Those
    are kept on disk (capture-everything) but must not reach verified.txt, the
    GPL download list consumed by s5cmd. Validated against the known corpus:
    keeps all GPL tarballs, drops all non-GPL assets. See docs/GPL-RECON.md.

    Args:
        url: A canonical URL (output of url_normalize).

    Returns:
        True if the URL looks like a GPL archive and should be verified.

    """
    low = url.lower()
    return "gpl" in low and low.endswith(_GPL_ARCHIVE_EXTS)


def read_all_txt(scrapes_dir: Path) -> set[str]:
    """Read every .txt file in scrapes_dir recursively and return a normalized URL set.

    Processes seed/ and all timestamped run directories in a single recursive
    glob pass (Path.rglob — not Path.glob, which is non-recursive). Each URL
    line is normalized via url_normalize before insertion so duplicate encodings
    of the same S3 key collapse to one entry.

    Skips blank lines silently. On unreadable files, logs a warning and continues
    to the next file — never uses return (which would silently truncate results).

    Per VERF-02 (reads all .txt files under scrapes/), VERF-03 (normalizes and
    deduplicates), DIR-04 (covers seed/ + timestamped dirs), DIR-01 (never writes
    to seed/).

    Args:
        scrapes_dir: Root scrapes directory. Must exist and be readable.
            Typically data/scrapes/ relative to the data root.

    Returns:
        Set of canonical HTTPS URLs (output of url_normalize). May be empty if
        no .txt files are found or all files are unreadable.

    """
    urls: set[str] = set()
    for txt_path in scrapes_dir.rglob("*.txt"):
        try:
            for line in txt_path.read_text(encoding="utf-8").splitlines():
                norm = url_normalize(line)
                if norm:
                    urls.add(norm)
        except OSError:
            # One unreadable file must not truncate the entire result — use
            # continue, not return. This is the critical correctness distinction
            # (see RESEARCH.md Pitfall: return vs continue).
            _log.warning("Could not read %s, skipping", txt_path)
            continue
    return urls
