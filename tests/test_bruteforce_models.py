"""Tests for tpwalk/bruteforce/_models.py — pure-function extraction, corpus mining, and candidate format.

No HTTP, no MockTransport, no asyncio. Pure-function unit tests mirroring
the discipline of tests/test_normalize.py. Each test imports the function
under test locally to keep the import graph obvious.

BRUT-03: model extraction fixture, D-04 corpus loading + shape mining,
D-05 canonical template floor, and iter_model_candidates URL format.
"""

from __future__ import annotations

from pathlib import Path

# Fixture keys copied verbatim from RESEARCH Validation Architecture.
_FIXTURE_KEYS = [
    "firmware/AP9650v1_1.1.1_[20250804-rel72981]_up_signed_1758134504992.bin",
    "firmware/(B)AP9670v1_1.1.1_[20250804-rel72981]_up_signed_1758133273408.bin",
    "firmware/1.0.0 Build 140808 Rel.58168n_1445420381134.bin",  # should be skipped
    "firmware/TL-WR841Nv14_US_0.9.1_up_signed_1234567890123.bin",
]


# ---------------------------------------------------------------------------
# Task 1: load_firmware_keys — dict-array shape extraction
# ---------------------------------------------------------------------------


def test_load_firmware_keys_extracts_key_strings(tmp_path: Path) -> None:
    """load_firmware_keys must extract entry['key'] strings, NOT return the raw dict list."""
    import json

    from tpwalk.bruteforce._models import load_firmware_keys

    data = [
        {"key": "firmware/X.bin", "size": 1, "modified": "2024-01-01T00:00:00.000Z"},
        {"key": "app/", "size": 0, "modified": "2024-01-01T00:00:00.000Z"},
    ]
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps(data), encoding="utf-8")

    result = load_firmware_keys(listing_path=listing)

    assert result == ["firmware/X.bin", "app/"]
    assert isinstance(result[0], str), "returned entries must be strings, not dicts"


# ---------------------------------------------------------------------------
# Task 1: extract_firmware_models — recall-favoring fixture
# ---------------------------------------------------------------------------


def test_model_extraction_fixture() -> None:
    """extract_firmware_models returns expected tokens for known fixture keys (BRUT-03)."""
    from tpwalk.bruteforce._models import extract_firmware_models

    result = extract_firmware_models(_FIXTURE_KEYS)
    assert "AP9650v1" in result
    assert "AP9670v1" in result
    assert "TL-WR841Nv14" in result
    assert "TL-WR841N" in result  # de-versioned form
    # Version-only entry must be skipped
    assert not any("1.0.0" in t for t in result)


def test_model_extraction_strips_classification_prefix() -> None:
    """(B) classification prefix is stripped; AP9670v1 still extracted."""
    from tpwalk.bruteforce._models import extract_firmware_models

    result = extract_firmware_models(["firmware/(B)AP9670v1_1.1.0_[20250101-rel1].bin"])
    assert "AP9670v1" in result


def test_model_extraction_skips_version_only() -> None:
    """Firmware keys that are pure version strings are skipped."""
    from tpwalk.bruteforce._models import extract_firmware_models

    result = extract_firmware_models(["firmware/1.0.0 Build 140808 Rel.58168n_123.bin"])
    assert len(result) == 0


def test_model_extraction_skips_non_firmware_keys() -> None:
    """Non-firmware/ keys (e.g. app/foo.ipa) contribute no tokens."""
    from tpwalk.bruteforce._models import extract_firmware_models

    result = extract_firmware_models(["app/foo.ipa", "other/bar.bin"])
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Task 2: load_known_basenames — percent-decode corpus
# ---------------------------------------------------------------------------


def test_load_known_basenames_decodes_percent_encoding(tmp_path: Path) -> None:
    """load_known_basenames decodes percent-encoded basenames (D-04, 49 encoded names)."""
    from tpwalk.bruteforce._models import load_known_basenames

    corpus = tmp_path / "corpus.txt"
    corpus.write_text(
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz\nhttps://static.tp-link.com/resources/gpl/Archer%20A10%28US%292.0-GPL.tar.gz\n",
        encoding="utf-8",
    )

    result = load_known_basenames(corpus_path=corpus)

    assert result == ["11N_GPL.tgz", "Archer A10(US)2.0-GPL.tar.gz"]


# ---------------------------------------------------------------------------
# Task 2: derive_corpus_patterns — D-04 empirical shape mining
# ---------------------------------------------------------------------------


def test_derive_corpus_patterns_dash_separator() -> None:
    """Dash-separated lowercase: '1201-gpl.tar.gz' → '{model}-gpl'."""
    from tpwalk.bruteforce._models import derive_corpus_patterns

    result = derive_corpus_patterns(["1201-gpl.tar.gz"])
    assert "{model}-gpl" in result


def test_derive_corpus_patterns_suffix_token() -> None:
    """Suffix token _gpl_src: 'MR500V1_gpl_src.tar.gz' → '{model}_gpl_src'."""
    from tpwalk.bruteforce._models import derive_corpus_patterns

    result = derive_corpus_patterns(["MR500V1_gpl_src.tar.gz"])
    assert "{model}_gpl_src" in result


def test_derive_corpus_patterns_suffix_uppercase() -> None:
    """Uppercase suffix pattern: 'A10(JP)V1_GPL.tar.bz2' → '{model}_GPL'."""
    from tpwalk.bruteforce._models import derive_corpus_patterns

    result = derive_corpus_patterns(["A10(JP)V1_GPL.tar.bz2"])
    assert "{model}_GPL" in result


def test_derive_corpus_patterns_prefix_uppercase() -> None:
    """Uppercase prefix pattern: 'GPL_Foo.tar.gz' → 'GPL_{model}'."""
    from tpwalk.bruteforce._models import derive_corpus_patterns

    result = derive_corpus_patterns(["GPL_Foo.tar.gz"])
    assert "GPL_{model}" in result


def test_derive_corpus_patterns_gpl_less_bare_model() -> None:
    """gpl-less basenames emit the bare '{model}' pattern (165 such basenames in corpus)."""
    from tpwalk.bruteforce._models import derive_corpus_patterns

    result = derive_corpus_patterns(["840v5.tar.gz", "150Router.rar"])
    assert "{model}" in result


def test_derive_corpus_patterns_canonical_floor_on_empty() -> None:
    """derive_corpus_patterns([]) still returns the 3 D-05 canonical templates."""
    from tpwalk.bruteforce._models import derive_corpus_patterns

    result = derive_corpus_patterns([])
    assert {"GPL_{model}", "{model}_GPL", "{model}_gpl"}.issubset(result)


def test_derive_corpus_patterns_deterministic() -> None:
    """Two calls on the same input return equal sets (no randomness, CLAUDE.md)."""
    from tpwalk.bruteforce._models import derive_corpus_patterns

    inputs = ["1201-gpl.tar.gz", "MR500V1_gpl_src.tar.gz", "GPL_Foo.tar.gz", "840v5.tar.gz"]
    assert derive_corpus_patterns(inputs) == derive_corpus_patterns(inputs)


# ---------------------------------------------------------------------------
# Task 3: iter_model_candidates — URL format + pattern union + extension coverage
# ---------------------------------------------------------------------------


def test_iter_model_candidates_canonical_flat_urls() -> None:
    """Default patterns=None, date_paths=None yields the 3 canonical-shape flat URLs."""
    from tpwalk.bruteforce._models import iter_model_candidates

    results = set(iter_model_candidates({"TL-WR841N"}))
    assert "https://static.tp-link.com/resources/gpl/GPL_TL-WR841N.tar.gz" in results
    assert "https://static.tp-link.com/resources/gpl/TL-WR841N_GPL.tar.gz" in results
    assert "https://static.tp-link.com/resources/gpl/TL-WR841N_gpl.tar.gz" in results


def test_iter_model_candidates_empirical_shapes() -> None:
    """Explicit empirical-shape patterns yield expected non-canonical flat URLs (D-04)."""
    from tpwalk.bruteforce._models import iter_model_candidates

    results = set(
        iter_model_candidates(
            {"TL-WR841N"},
            patterns=("{model}-GPL", "{model}_gpl_src", "{model}"),
        )
    )
    # Dash separator (non-canonical)
    assert "https://static.tp-link.com/resources/gpl/TL-WR841N-GPL.tar.gz" in results
    # Suffix token (non-canonical)
    assert "https://static.tp-link.com/resources/gpl/TL-WR841N_gpl_src.tar.gz" in results
    # gpl-less bare model (non-canonical)
    assert "https://static.tp-link.com/resources/gpl/TL-WR841N.tar.gz" in results


def test_iter_model_candidates_default_count_exceeds_canonical() -> None:
    """Default _ALL_PATTERNS produces per-model count > 24 (D-06 over-inclusive; D-04)."""
    from tpwalk.bruteforce._models import _ALL_PATTERNS, iter_model_candidates

    count = len(list(iter_model_candidates({"X"})))
    assert count > 24, f"expected > 24 candidates per model, got {count}"
    assert count == len(_ALL_PATTERNS) * 8


def test_iter_model_candidates_extensions_include_bare_gz_bz2() -> None:
    """1-pattern tuple produces 8 URLs including bare .gz and .bz2 (Pitfall 6)."""
    from tpwalk.bruteforce._models import iter_model_candidates

    results = list(iter_model_candidates({"M"}, patterns=("{model}_GPL",)))
    assert len(results) == 8
    urls = set(results)
    assert "https://static.tp-link.com/resources/gpl/M_GPL.gz" in urls
    assert "https://static.tp-link.com/resources/gpl/M_GPL.bz2" in urls


def test_iter_model_candidates_flat_default_no_date_path() -> None:
    """date_paths=None default: all URLs use /resources/gpl/, none use /upload/gpl-code/ (D-01)."""
    from tpwalk.bruteforce._models import iter_model_candidates

    results = list(iter_model_candidates({"TL-WR841N"}))
    assert all("/resources/gpl/" in u for u in results)
    assert not any("/upload/gpl-code/" in u for u in results)


def test_iter_model_candidates_exhaustive_date_cross() -> None:
    """date_paths provided: empirical shape crosses date path (--exhaustive branch, D-04+D-01)."""
    from tpwalk.bruteforce._models import iter_model_candidates

    date_paths = ["/upload/gpl-code/2023/202301/20230101/"]
    results = set(
        iter_model_candidates(
            {"TL-WR841N"},
            patterns=("{model}-GPL",),
            date_paths=date_paths,
        )
    )
    assert "https://static.tp-link.com/upload/gpl-code/2023/202301/20230101/TL-WR841N-GPL.tar.gz" in results


# ---------------------------------------------------------------------------
# WR-04 regression: corpus recall floor + cwd independence
# ---------------------------------------------------------------------------


def test_all_patterns_has_minimum_count_from_real_corpus() -> None:
    """_ALL_PATTERNS yields >= 20 patterns when the real corpus is present (WR-02 / D-04).

    This pins the recall floor: if _resolve_ref_file cannot find the corpus, the
    module falls back to 3 D-05 canonical patterns.  With the real corpus present,
    the empirical shape mining always produces more than 20.

    If this test runs without the real ref_gpl_data/ corpus (e.g., in a stripped
    Docker image), it verifies at least the canonical floor of 3.  The >= 20
    assertion is the meaningful guard — it catches the WR-02 regression where
    running from a non-project-root cwd silently degraded to 3 patterns.
    """
    from pathlib import Path

    from tpwalk.bruteforce._models import _ALL_PATTERNS, _resolve_ref_file

    corpus_path = _resolve_ref_file("gpl_urls_master.txt")
    if corpus_path is not None and Path(corpus_path).exists():
        assert len(_ALL_PATTERNS) >= 20, f"Expected >= 20 corpus-mined patterns with real corpus present, got {len(_ALL_PATTERNS)}. This likely means _ALL_PATTERNS was computed with cwd != project root (WR-02 regression)."
    else:
        # Corpus absent in this environment: canonical floor must still hold.
        assert len(_ALL_PATTERNS) == 3, f"Expected 3 canonical patterns when corpus absent, got {len(_ALL_PATTERNS)}"


def test_resolve_ref_file_finds_corpus_independent_of_cwd(tmp_path: Path) -> None:
    """_resolve_ref_file locates ref_gpl_data/ via __file__-relative path regardless of cwd (WR-02).

    We change the process cwd to a temp dir that has NO ref_gpl_data/ subdirectory
    and verify that _resolve_ref_file still resolves the real corpus file via the
    __file__ fallback (or returns None if the corpus is absent from the environment).

    This pins the fix for WR-02: before the fix, _DEFAULT_CORPUS_PATH was a bare
    relative Path that resolved against cwd, returning not-found from any non-project-root dir
    even when the corpus was accessible via __file__-relative path.
    """
    import os

    from tpwalk.bruteforce._models import _resolve_ref_file

    # tmp_path is a fresh empty directory with NO ref_gpl_data/ subdirectory.
    orig_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)  # cwd is now a temp dir with no ref_gpl_data/

        # Verify the cwd-relative path does NOT exist (sanity check).
        cwd_corpus = Path("ref_gpl_data/gpl_urls_master.txt")
        assert not cwd_corpus.exists(), "tmp_path unexpectedly has ref_gpl_data/ (test setup error)"

        result = _resolve_ref_file("gpl_urls_master.txt")
        # If the __file__-relative corpus exists (normal dev environment), result must be
        # the __file__-relative resolved path, NOT a cwd-relative path (which doesn't exist).
        # If the corpus is absent from both locations, None is acceptable.
        if result is not None:
            # The result must be an absolute resolved path that actually exists.
            assert result.exists(), f"_resolve_ref_file returned a non-existent path: {result}"
            # The result must NOT be the cwd-relative path (which we verified doesn't exist).
            assert result.is_absolute(), f"Expected absolute path, got relative: {result}"
    finally:
        os.chdir(orig_cwd)


def test_load_firmware_keys_rejects_malformed_structures(tmp_path: Path) -> None:
    """load_firmware_keys raises ValueError for structurally wrong but valid JSON (WR-03).

    Covers the four cases from the review finding:
      1. Top-level dict -> ValueError.
      2. List of nulls -> returns empty list (no crash).
      3. List of dicts missing 'key' -> returns empty list (no crash).
      4. List of bare strings -> returns empty list (no crash).
    """
    import json

    from tpwalk.bruteforce._models import load_firmware_keys

    # Case 1: top-level dict raises ValueError
    f1 = tmp_path / "dict.json"
    f1.write_text(json.dumps({"key": "firmware/X.bin"}), encoding="utf-8")
    try:
        load_firmware_keys(listing_path=f1)
        raise AssertionError("Expected ValueError for top-level dict, got no exception")
    except ValueError:
        pass  # expected

    # Case 2: list of nulls -> empty result, no crash
    f2 = tmp_path / "nulls.json"
    f2.write_text(json.dumps([None, None]), encoding="utf-8")
    result2 = load_firmware_keys(listing_path=f2)
    assert result2 == [], f"Expected [] for list of nulls, got {result2}"

    # Case 3: list of dicts missing 'key' -> empty result
    f3 = tmp_path / "no_key.json"
    f3.write_text(json.dumps([{"size": 1, "modified": "2024-01-01"}]), encoding="utf-8")
    result3 = load_firmware_keys(listing_path=f3)
    assert result3 == [], f"Expected [] for dicts missing 'key', got {result3}"

    # Case 4: list of bare strings -> empty result (strings are not dicts)
    f4 = tmp_path / "strings.json"
    f4.write_text(json.dumps(["firmware/X.bin", "firmware/Y.bin"]), encoding="utf-8")
    result4 = load_firmware_keys(listing_path=f4)
    assert result4 == [], f"Expected [] for list of bare strings, got {result4}"
