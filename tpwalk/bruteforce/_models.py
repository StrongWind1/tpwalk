"""Model-name candidate generator for the brute-force model strategy.

Model tokens are extracted at runtime from data/firmware_s3_listing.json
with a recall-favoring heuristic — over-inclusive is correct because 404s are free
(D-06). The firmware listing is a JSON array of {key, size, modified} objects (NOT
a flat list of strings); load_firmware_keys extracts entry["key"] from each dict.

The candidate filename-pattern set is the UNION of:
  - D-05 canonical templates: GPL_{model}, {model}_GPL, {model}_gpl
  - D-04 empirically-mined shapes derived from the 1,283 known basenames in
    data/scrapes/seed/gpl_urls_master.txt (dash/underscore/glued separators, prefix/suffix
    position, GPL/gpl case, _src/_code/_OpenSource suffix tokens, gpl-less bare model)

The model dimension is ORTHOGONAL to the date dimension (D-01): iter_model_candidates
defaults to the flat /resources/gpl/ prefix with no date cross-product. The date
cross-product is available only as an explicit opt-in for --exhaustive mode.

Per BRUT-02 (model-name candidate generation), BRUT-03 (firmware model names x GPL
filename patterns), D-01 (orthogonal), D-04 (empirical corpus patterns), D-05
(canonical template floor x 8 extensions), D-06 (runtime recall-favoring extraction).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

if TYPE_CHECKING:
    from collections.abc import Iterator

_log = logging.getLogger(__name__)

# --- Module-level compiled regex constants ---

# Region-code suffix regex: strips country/region code before the version separator.
# Covers the two-letter country codes observed in firmware_s3_listing.json.
# Per RESEARCH Pattern 3.
_REGION_SUFFIX_RE = re.compile(
    r"_(?:JP|US|EU|AU|UK|KR|TW|CN|IN|SA|RU|CA|UN|BR|MX|AR|DE|FR|ES|NL|"
    r"SE|PL|SG|MY|TH|PH|ID|VN|HK|NZ|NO|DK|FI|PT|BE|CH|GR|HU|RO|AT|CZ|CL|CO)"
    r"(?=[-_]|$)",
    re.IGNORECASE,
)

# Version separator regex: splits the model name from the firmware version/build info.
# Per RESEARCH Pattern 3.
_VERSION_SEP_RE = re.compile(
    r"(?:_\d+\.\d+"  # _X.Y firmware version
    r"|-[Uu][Pp]-"  # -UP- OEM descriptor
    r"|-[Uu][Pp][Vv]"  # -UPV / -upv
    r"|_\[20"  # _[20xx build info
    r"|-\[20"  # -[20xx build info
    r"|_20\d{6}"  # _20YYMMDD date
    r"|-20\d{2}-\d{2}-\d{2})",  # -2023-01-01 date
)

# Extension strip regex: removes the trailing archive extension from a corpus basename.
# Used in extract_firmware_models (firmware keys) and derive_corpus_patterns (corpus basenames).
# Longest alternations listed first so "tar.gz" matches before "gz".
_EXT_STRIP_RE = re.compile(
    r"\.(bin|tar\.gz|tar\.bz2|zip|tar|gz|bz2|rar|tgz|rollback)$",
    re.IGNORECASE,
)

# Trailing date/version noise pattern: collapse _20190213, _V2, _2 etc. to nothing
# so the emitted pattern generalizes across per-file variants.
# E.g. A20v1_US_GPL_20190213 -> model=A20v1, suffix= (date noise dropped).
_CORPUS_NOISE_RE = re.compile(r"[_-]\d{8}$|[_-][Vv]\d+$|[_-]\d+$")

# Recognized trailing suffix tokens after the gpl token in corpus basenames.
# These are the raw strings AFTER the gpl token itself (e.g. for MR500V1_gpl_src,
# remainder after "gpl" is "_src"). Preserved in the emitted pattern.
# E.g. {model}_gpl + _src -> "{model}_gpl_src".
# Per D-04 corpus shape analysis (_code: 65, _src: 11, _OpenSource: 4).
_CORPUS_KNOWN_SUFFIXES: tuple[str, ...] = ("_src", "_code", "_SRC", "_CODE", "_OpenSource", "_opensource")

# --- Archive extensions (D-05 canonical set, Pitfall 6: include bare gz/bz2) ---
# The 8 known archive extensions observed in gpl_urls_master.txt.
# Per D-05; bare gz/bz2 included per Pitfall 6 from RESEARCH.
_EXTENSIONS: tuple[str, ...] = ("tar.gz", "tar.bz2", "tar", "tgz", "zip", "rar", "gz", "bz2")

# --- D-05 canonical pattern templates (the floor — always present) ---
# These three shapes account for ~82% of known GPL filenames and are the
# guaranteed minimum pattern set regardless of corpus availability.
_CANONICAL_PATTERNS: tuple[str, ...] = ("GPL_{model}", "{model}_GPL", "{model}_gpl")

# Minimum length for a valid model name token (avoids 1-2 char noise tokens).
_MIN_MODEL_LEN = 3

# --- Shared data/ reference-file resolver ---
# Resolves a path under data/ by trying cwd first, then __file__-relative to the repo
# root, so behavior is identical regardless of the working directory the process was
# started from (WR-02: a console-script user is NOT necessarily at project root).
# _models.py lives at tpwalk/bruteforce/_models.py, so three parents up reaches the
# project root that contains data/.
_DATA_ROOT = Path(__file__).parent.parent.parent / "data"


def _resolve_data_file(relpath: str) -> Path | None:
    """Resolve a file under data/ by relative path: cwd-relative first, then repo-root-relative.

    Returns the first existing Path, or None if neither location has the file. The
    repo-root fallback keeps resolution cwd-independent — without it, _ALL_PATTERNS
    silently degrades to the D-05 canonical floor and the model strategy loses recall
    whenever tpwalk is invoked from outside the project root (WR-02).

    Args:
        relpath: Path relative to data/ (e.g. "scrapes/seed/gpl_urls_master.txt").

    Returns:
        Resolved Path if the file exists, else None.

    """
    cwd_path = Path("data") / relpath
    if cwd_path.exists():
        return cwd_path
    root_path = _DATA_ROOT / relpath
    if root_path.exists():
        return root_path
    return None


# --- Default corpus path for _ALL_PATTERNS computation (relative to data/) ---
# The GPL URL corpus doubles as the verify seed, so it lives at data/scrapes/seed/.
_DEFAULT_CORPUS_RELPATH = "scrapes/seed/gpl_urls_master.txt"


# --- Public functions ---


def load_firmware_keys(*, listing_path: Path) -> list[str]:
    """Load firmware S3 key strings from the JSON listing file.

    The listing is a JSON array of {key, size, modified} dicts (NOT a flat
    list of strings). This function extracts the "key" field from each dict.

    CRITICAL DATA SHAPE: data/firmware_s3_listing.json contains
    53,857 entries as [{"key": "...", "size": N, "modified": "..."}, ...].
    Returning the raw data would return a list of dicts — this function maps
    entry["key"] out of each dict and returns a plain list[str].

    Structurally malformed but syntactically valid JSON (top-level dict, list of
    nulls, entries missing "key") raises ValueError so the caller's
    except (OSError, ValueError) handler degrades to models=set() instead of crashing.

    Per D-06 (runtime extraction from firmware_s3_listing.json).

    Args:
        listing_path: Path to the firmware S3 listing JSON file.

    Returns:
        List of S3 key strings, filtered to well-formed entries only.

    Raises:
        OSError: If listing_path cannot be read.
        ValueError: If the JSON top-level is not a list.

    """
    data = json.loads(listing_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        # Raise ValueError (not TypeError) so the caller's except (OSError, ValueError) handler
        # catches this as a "malformed" condition and degrades to models=set() (WR-03).
        msg = "firmware listing must be a JSON array of objects"
        raise ValueError(msg)  # noqa: TRY004
    return [entry["key"] for entry in data if isinstance(entry, dict) and isinstance(entry.get("key"), str)]


def extract_firmware_models(fw_s3_keys: list[str]) -> set[str]:
    """Extract model name tokens from firmware S3 keys for GPL brute-force.

    Strategy: strip (B)/(C)/(EAP) classification prefix, epoch timestamp suffix,
    bare extension, region code, and version indicators to obtain the bare hardware
    model name. Both versioned (e.g. TL-WR841Nv14) and de-versioned (TL-WR841N)
    forms are included since GPL filenames use both.

    Recall-favoring: over-inclusive is correct — 404s are free (D-06). The
    extracted tokens will be inserted into GPL filename patterns; tokens that
    don't match real GPL files produce cheap 404s, not missed discoveries.

    The empirically-verified token count from the full 52,715 firmware/ keys
    is ~4,281 tokens (RESEARCH §"Confirmed Numbers"). The CONTEXT's "772 model
    names" is stale prose from prior work — the runtime extraction yields far
    more and that is intentional and correct (D-06, BRUT-03).

    Per BRUT-03, D-06.

    Args:
        fw_s3_keys: Raw key strings from firmware_s3_listing.json (already
            extracted via load_firmware_keys — these are strings, not dicts).

    Returns:
        Set of model name strings for use in GPL candidate URL construction.

    """
    models: set[str] = set()
    for key in fw_s3_keys:
        if not key.startswith("firmware/"):
            continue
        fname = key[len("firmware/") :]
        # Strip (B)/(C)/(EAP) classification prefix
        fname = re.sub(r"^\([^)]{1,8}\)", "", fname)
        # Skip version-only entries like "1.0.0 Build..."
        if re.match(r"^\d[\d.]*[\s_]", fname):
            continue
        # Strip timestamp suffix _<10-13 digit epoch>.<ext>
        fname = re.sub(r"_\d{10,13}(\.[a-z0-9]+)?$", "", fname, flags=re.IGNORECASE)
        # Strip bare extension and .rollback
        fname = re.sub(r"\.(bin|tar\.gz|zip|tar\.bz2|tar|gz|bz2|rar|rollback)$", "", fname, flags=re.IGNORECASE)
        # Strip region code suffix before version separator
        fname = _REGION_SUFFIX_RE.split(fname)[0]
        # Split at first version indicator to get just the model
        model = _VERSION_SEP_RE.split(fname, maxsplit=1)[0].strip("_-. ")
        if not model or len(model) < _MIN_MODEL_LEN or not re.search(r"[A-Za-z]", model):
            continue
        if re.match(r"^\d+phase", model, re.IGNORECASE):
            continue
        models.add(model)
        # Also add de-versioned form: TL-WR841Nv14 -> TL-WR841N
        stripped = re.sub(r"[Vv]\d+(\.\d+)*$", "", model).rstrip("._- ")
        if stripped and stripped != model and len(stripped) >= _MIN_MODEL_LEN:
            models.add(stripped)
    return models


def load_known_basenames(*, corpus_path: Path) -> list[str]:
    """Load and decode the known GPL URL corpus into a list of basenames.

    The corpus file (data/scrapes/seed/gpl_urls_master.txt) contains 1,283 full
    HTTPS URLs, one per line. 49 of those URLs have percent-encoded basenames
    (e.g. Archer%20A10%28US%292.0-GPL.tar.gz) — these are decoded via stdlib
    unquote so the shape-mining in derive_corpus_patterns sees the real characters.

    The returned list preserves order and retains duplicates (deduplication
    happens downstream in derive_corpus_patterns, which builds a set).

    Per D-04 (corpus-grounded empirical pattern mining from the 1,283 known
    basenames in gpl_urls_master.txt).

    Args:
        corpus_path: Path to the known GPL URLs text file (one URL per line).

    Returns:
        List of decoded basename strings (filename portion of each URL).

    """
    lines = corpus_path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        # Extract the basename — everything after the last "/"
        basename = line.rpartition("/")[2]
        result.append(unquote(basename))
    return result


def derive_corpus_patterns(basenames: list[str]) -> set[str]:
    """Mine the D-04 empirical filename-shape distribution from known GPL basenames.

    Produces a set of {model}-templated pattern strings by classifying each
    basename according to the observed shape (GPL-token position prefix/suffix/absent,
    adjacent separator _, -, or none/glued, case of the gpl token, recognized trailing
    suffix tokens). Each emitted shape is traceable to at least one real basename in
    the 1,283-URL corpus — no heuristic guessing.

    The D-05 canonical templates are always seeded first as the guaranteed floor so
    the generator never regresses below D-05 even when the corpus file is absent.

    Corpus shape statistics (RESEARCH §"Confirmed Numbers from Empirical Data Analysis"):

    - GPL token position: suffix 895 / prefix 223 / none 165
    - Separators: underscore (dominant), dash (~41), glued/none (~20)
    - Suffix tokens: _gpl_code/gpl_code (65), _gpl_src/gpl_src (11), _OpenSource (4)
    - Trailing date/version noise: dropped to generalize the pattern

    Per D-04 (empirically-mined from 1,283 known basenames), D-06 (over-inclusive
    is fine — 404s are free). Each emitted shape is corpus-grounded; the mining is
    deterministic (no randomness, no _looks_reasonable scoring).

    Args:
        basenames: List of decoded GPL archive basenames (output of load_known_basenames
            or a small list for testing). The D-05 floor is always included; an empty
            list returns only the three canonical templates.

    Returns:
        Deduplicated set of {model}-templated pattern strings. The D-05 templates
        (GPL_{model}, {model}_GPL, {model}_gpl) are always present.

    """
    patterns: set[str] = set(_CANONICAL_PATTERNS)

    for basename in basenames:
        # Strip the trailing archive extension to get the stem.
        stem = _EXT_STRIP_RE.sub("", basename)

        # Locate a case-insensitive 'gpl' token in the stem.
        gpl_match = re.search(r"gpl", stem, re.IGNORECASE)

        if gpl_match is None:
            # 165 gpl-less basenames in corpus: emit bare {model} pattern.
            # Check for _OpenSource / _opensource shaped basenames (4 such files).
            if stem.endswith("_OpenSource"):
                patterns.add("{model}_OpenSource")
            elif stem.endswith("_opensource"):
                patterns.add("{model}_opensource")
            else:
                patterns.add("{model}")
            continue

        # Capture the exact matched gpl substring (preserves case: GPL vs gpl vs Gpl).
        gpl_token = gpl_match.group(0)
        gpl_start = gpl_match.start()
        gpl_end = gpl_match.end()

        is_prefix = gpl_start == 0

        if is_prefix:
            # Prefix position: gpl_token comes before the model name.
            # Inspect the separator char immediately after the gpl token.
            after = stem[gpl_end : gpl_end + 1]
            sep = after if after in ("_", "-") else ""
            patterns.add(f"{gpl_token}{sep}{{model}}")
        else:
            # Suffix position: model name comes before the gpl token.
            # Inspect the separator char immediately before the gpl token.
            before = stem[gpl_start - 1 : gpl_start] if gpl_start > 0 else ""
            sep = before if before in ("_", "-") else ""

            # Capture the remainder after the gpl token (potential suffix tokens).
            remainder = stem[gpl_end:]

            # Drop trailing date/version noise to generalize the pattern
            # (e.g. _20190213, _V2, _2 are per-file variants, not shape).
            remainder = _CORPUS_NOISE_RE.sub("", remainder)

            # Check for recognized suffix tokens (case-sensitive, literal match).
            # _CORPUS_KNOWN_SUFFIXES are the raw strings AFTER the gpl token itself:
            # "_src" (not "_gpl_src") since the gpl_token is captured separately.
            suffix_token = ""
            for known in _CORPUS_KNOWN_SUFFIXES:
                if remainder == known:
                    suffix_token = known
                    break

            if suffix_token:
                patterns.add(f"{{model}}{sep}{gpl_token}{suffix_token}")
            else:
                patterns.add(f"{{model}}{sep}{gpl_token}")

    return patterns


# --- Module-level D-04 + D-05 pattern union ---
# Computed at import time via _resolve_data_file() which tries cwd then repo-root-relative,
# so the result is correct regardless of the operator's cwd (WR-02 fix: the old code used
# a bare relative Path that only worked when cwd == project root).
# Wrapped in try/except so import never crashes if the corpus is absent.
# Per D-04 (empirical corpus patterns), D-05 (canonical floor).

_corpus_path_for_patterns = _resolve_data_file(_DEFAULT_CORPUS_RELPATH)
if _corpus_path_for_patterns is not None:
    try:
        _ALL_PATTERNS: tuple[str, ...] = tuple(sorted(derive_corpus_patterns(load_known_basenames(corpus_path=_corpus_path_for_patterns))))
    except OSError, ValueError:
        _log.warning(
            "data/%s malformed — falling back to D-05 canonical patterns only (recall reduced).",
            _DEFAULT_CORPUS_RELPATH,
        )
        _ALL_PATTERNS = tuple(sorted(_CANONICAL_PATTERNS))
else:
    _log.warning(
        "data/%s not found — falling back to D-05 canonical patterns only (recall reduced). Place the GPL URL corpus there to restore D-04 empirical shapes.",
        _DEFAULT_CORPUS_RELPATH,
    )
    _ALL_PATTERNS = tuple(sorted(_CANONICAL_PATTERNS))


def iter_model_candidates(
    models: set[str],
    *,
    patterns: tuple[str, ...] | None = None,
    date_paths: list[str] | None = None,
) -> Iterator[str]:
    """Yield candidate GPL archive URLs for each model token x pattern x extension.

    Default behavior (date_paths=None): targets the flat /resources/gpl/ prefix.
    The model dimension is ORTHOGONAL to the date dimension (D-01) — this is the
    correct default for the model strategy because a GPL filename lives at exactly
    one upload date, so multiplying every model-pattern by every date produces
    ~1,799 guaranteed misses per 1,800 guesses.

    The default pattern set is _ALL_PATTERNS (the D-04 corpus-mined empirical shapes
    unioned with the D-05 canonical templates). The per-model candidate count is
    intentionally larger than the canonical-only 24 (len(_ALL_PATTERNS) x 8 per model)
    because D-06 endorses over-inclusive enumeration — 404s are free.

    Opt-in exhaustive branch (date_paths provided): crosses each model-pattern with
    each date-path prefix to produce hierarchical /upload/gpl-code/ URLs. This is
    the only place where model patterns meet date paths, and only when explicitly
    requested by the runner for --exhaustive mode.

    Per BRUT-02, BRUT-03, D-01 (default flat = orthogonal; exhaustive is opt-in),
    D-04 (empirical shape union is the default pattern set), D-05 (canonical floor),
    D-06 (over-inclusive is correct — 404s are free), D-11 (canonical https:// URLs).

    Args:
        models: Set of model name token strings (output of extract_firmware_models).
        patterns: Optional explicit pattern tuple for testability. If None, the
            module default _ALL_PATTERNS (D-04 + D-05 union) is used.
        date_paths: If None (default and --thorough for model strategy), yields URLs
            on the flat /resources/gpl/ prefix. If provided (--exhaustive only),
            crosses each model-pattern with each date-path string.

    Yields:
        Full canonical https://static.tp-link.com URLs, one per
        (model, pattern, extension) combination — or (model, pattern, date_path,
        extension) in the exhaustive branch.

    """
    pats = patterns if patterns is not None else _ALL_PATTERNS
    for model in models:
        for pattern in pats:
            stem = pattern.format(model=model)
            for ext in _EXTENSIONS:
                if date_paths is None:
                    yield f"https://static.tp-link.com/resources/gpl/{stem}.{ext}"
                else:
                    for path in date_paths:
                        yield f"https://static.tp-link.com{path}{stem}.{ext}"
