"""Tests for tpwalk.scrape._regional -- productTree extraction, phppage URL parsing, HTML fallback, region discovery.

Tests use inline HTML fixtures and httpx.MockTransport so no real network requests are made.
Per SCRP-02, SCRP-03, D-01, D-02.
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx

# --- Tests for _extract_product_tree ---


def test_extract_product_tree_dict_form() -> None:
    """productTree dict-form page returns parsed dict keyed by category ID (SCRP-02, D-05)."""
    from tpwalk.scrape._regional import _extract_product_tree

    html = '<html><head></head><body><script>var productTree = {"1": [{"href": "https://static.tp-link.com/a.tgz", "app_folder": "us", "model_name": "Archer AX50"}],"2": [{"href": "?model=Deco BE95", "app_folder": "us", "model_name": "Deco BE95"}]};</script></body></html>'
    result = _extract_product_tree(html)
    assert isinstance(result, dict)
    assert "1" in result
    assert "2" in result
    assert len(result["1"]) == 1
    assert result["1"][0]["href"] == "https://static.tp-link.com/a.tgz"


def test_extract_product_tree_empty_list() -> None:
    """productTree empty-list (var productTree = [];) returns None without error (Pitfall 1)."""
    from tpwalk.scrape._regional import _extract_product_tree

    html = "<html><script>var productTree = [];</script></html>"
    result = _extract_product_tree(html)
    assert result is None


def test_extract_product_tree_no_variable() -> None:
    """Page with no productTree variable returns None."""
    from tpwalk.scrape._regional import _extract_product_tree

    html = "<html><script>var someOtherVar = 1;</script></html>"
    result = _extract_product_tree(html)
    assert result is None


def test_extract_product_tree_unterminated_json() -> None:
    """Unterminated JSON after productTree marker returns None gracefully."""
    from tpwalk.scrape._regional import _extract_product_tree

    html = '<html><script>var productTree = {"1": [{"href": "x"}'
    result = _extract_product_tree(html)
    assert result is None


# --- Tests for _classify_tree_items ---


def test_classify_tree_items_splits_direct_and_phppage() -> None:
    """Items split into direct URLs (href starts with https://static.tp-link.com/) and phppage pairs (href starts with ?model=) per D-07, Pitfall 3, Pitfall 6."""
    from tpwalk.scrape._regional import _classify_tree_items

    tree: dict[str, list[dict]] = {
        "1": [
            {"href": "https://static.tp-link.com/upload/gpl-code/2024/archive.tar.gz", "app_folder": "us", "model_name": "Archer AX50"},
            {"href": "?model=Deco BE95", "app_folder": "de", "model_name": "Deco BE95"},
        ],
        "2": [
            {"href": "https://static.tp-link.com/resources/gpl/old_archive.tgz", "app_folder": "uk", "model_name": "TL-WR740N"},
        ],
    }
    direct_urls, phppage_pairs = _classify_tree_items(tree)
    assert len(direct_urls) == 2
    assert "https://static.tp-link.com/upload/gpl-code/2024/archive.tar.gz" in direct_urls
    assert "https://static.tp-link.com/resources/gpl/old_archive.tgz" in direct_urls
    assert len(phppage_pairs) == 1
    assert phppage_pairs[0] == ("Deco BE95", "de")


def test_classify_tree_items_skips_unrecognized_href() -> None:
    """Items with empty or unrecognized href values are skipped silently."""
    from tpwalk.scrape._regional import _classify_tree_items

    tree: dict[str, list[dict]] = {
        "1": [
            {"href": "", "app_folder": "us", "model_name": "Empty"},
            {"href": "/some/relative/path", "app_folder": "us", "model_name": "Relative"},
            {"href": "https://other-domain.com/file.tar.gz", "app_folder": "us", "model_name": "OtherDomain"},
            {"href": "https://static.tp-link.com/valid.tar.gz", "app_folder": "us", "model_name": "Valid"},
        ],
    }
    direct_urls, phppage_pairs = _classify_tree_items(tree)
    assert len(direct_urls) == 1
    assert direct_urls[0] == "https://static.tp-link.com/valid.tar.gz"
    assert len(phppage_pairs) == 0


# --- Tests for _extract_phppage_urls ---


def test_extract_phppage_urls_finds_static_urls() -> None:
    """phppage HTML fragment yields static.tp-link.com URLs via regex (SCRP-03)."""
    from tpwalk.scrape._regional import _extract_phppage_urls

    fragment = '<table class="list"><tr><td><a href="https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/Archer_AX50_GPL.tar.gz">Download</a></td></tr><tr><td><a href="https://static.tp-link.com/resources/gpl/Archer_AX50_v1_GPL.tgz">Download</a></td></tr></table>'
    result = _extract_phppage_urls(fragment)
    assert len(result) == 2
    assert "https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/Archer_AX50_GPL.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/Archer_AX50_v1_GPL.tgz" in result


def test_extract_phppage_urls_empty_body() -> None:
    """Empty/whitespace-only phppage response returns empty list (phppage empty sentinel)."""
    from tpwalk.scrape._regional import _extract_phppage_urls

    assert _extract_phppage_urls("") == []
    assert _extract_phppage_urls("   ") == []
    assert _extract_phppage_urls("\n\t") == []


# --- Tests for _extract_html_fallback_urls ---


def test_extract_html_fallback_urls_archive_only() -> None:
    """HTML fallback extracts only archive-extension URLs from the page (D-06)."""
    from tpwalk.scrape._regional import _extract_html_fallback_urls

    html = (
        "<html><body>"
        '<a href="https://static.tp-link.com/upload/gpl-code/2023/ACS_Server_GPL.tar.gz">GPL</a>'
        '<a href="https://static.tp-link.com/resources/gpl/old_gpl.zip">GPL</a>'
        '<img src="https://static.tp-link.com/images/product.jpg">'
        '<link href="https://static.tp-link.com/css/style.css">'
        '<script src="https://static.tp-link.com/js/app.js"></script>'
        "</body></html>"
    )
    result = _extract_html_fallback_urls(html)
    assert len(result) == 2
    assert "https://static.tp-link.com/upload/gpl-code/2023/ACS_Server_GPL.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/old_gpl.zip" in result


def test_extract_html_fallback_urls_ignores_non_archive() -> None:
    """Non-archive URLs from the same domain are filtered out."""
    from tpwalk.scrape._regional import _extract_html_fallback_urls

    html = '<a href="https://static.tp-link.com/image.png">img</a><a href="https://static.tp-link.com/doc.pdf">doc</a>'
    result = _extract_html_fallback_urls(html)
    assert result == []


def test_extract_phppage_urls_preserves_filename_spaces() -> None:
    """Filenames with spaces are captured whole, not truncated at the first space.

    Regression for the OC300 / TL-MR110(EU) failure: the live phppage serves
    href="https://static.tp-link.com/.../OC300 1.20.7z" and the old bare-URL
    regex (which stopped at \\s) truncated it to '.../OC300', a 403-dead key.
    """
    from tpwalk.scrape._regional import _extract_phppage_urls

    fragment = '<table class="list"><tr><td><a href="https://static.tp-link.com/upload/gpl-code/2026/202603/20260306/OC300 1.20.7z">Download</a></td></tr><tr><td><a href="https://static.tp-link.com/upload/gpl-code/2026/202601/20260113/TL-MR110(EU) 3.20_gpl_src.tar.gz">Download</a></td></tr></table>'
    result = _extract_phppage_urls(fragment)
    assert "https://static.tp-link.com/upload/gpl-code/2026/202603/20260306/OC300 1.20.7z" in result
    assert "https://static.tp-link.com/upload/gpl-code/2026/202601/20260113/TL-MR110(EU) 3.20_gpl_src.tar.gz" in result
    # No truncated stem must leak through.
    assert "https://static.tp-link.com/upload/gpl-code/2026/202603/20260306/OC300" not in result


def test_extract_html_fallback_urls_preserves_filename_spaces() -> None:
    """HTML fallback also keeps spaced archive filenames intact (same \\s bug)."""
    from tpwalk.scrape._regional import _extract_html_fallback_urls

    html = '<a href="https://static.tp-link.com/upload/gpl-code/2026/202603/20260306/OC300 1.20.7z">GPL</a>'
    result = _extract_html_fallback_urls(html)
    assert result == ["https://static.tp-link.com/upload/gpl-code/2026/202603/20260306/OC300 1.20.7z"]


def test_whitespace_padded_hrefs_are_recovered() -> None:
    """Leading/trailing whitespace in hrefs must not drop GPL URLs (Deco XE200 regression).

    TP-Link pads some productTree/phppage hrefs (e.g. href=" https://.../11.4.tar").
    The anchored startswith()/regex used to drop leading-space hrefs entirely. All
    three extraction paths must now recover them, with internal filename spaces
    preserved and surrounding pad trimmed.
    """
    from tpwalk.scrape._regional import _classify_tree_items, _extract_html_fallback_urls, _extract_phppage_urls

    # 1. productTree direct hrefs: one LEADING-space (the genuine drop), one trailing.
    tree: dict[str, list[dict]] = {
        "1": [
            {"href": " https://static.tp-link.com/upload/gpl-code/2022/202210/20221011/11.4.tar", "app_folder": "de", "model_name": "Deco XE200"},
            {"href": "https://static.tp-link.com/resources/gpl/Archer_AX90v1_GPL.tar.gz    ", "app_folder": "us", "model_name": "Archer AX90"},
        ],
    }
    direct, _ = _classify_tree_items(tree)
    assert "https://static.tp-link.com/upload/gpl-code/2022/202210/20221011/11.4.tar" in direct
    assert "https://static.tp-link.com/resources/gpl/Archer_AX90v1_GPL.tar.gz" in direct  # trailing pad trimmed

    # 2. phppage fragment with a leading-space href and an internal filename space.
    php = _extract_phppage_urls('<a href=" https://static.tp-link.com/upload/gpl-code/2026/OC300 1.20.7z">x</a>')
    assert "https://static.tp-link.com/upload/gpl-code/2026/OC300 1.20.7z" in php

    # 3. HTML fallback with a leading-space href.
    fb = _extract_html_fallback_urls('<a href=" https://static.tp-link.com/resources/gpl/GPL_M3W.tar.gz">x</a>')
    assert "https://static.tp-link.com/resources/gpl/GPL_M3W.tar.gz" in fb


# --- Tests for ScrapeStats ---


def test_scrape_stats_frozen_dataclass() -> None:
    """ScrapeStats is frozen and has all six required fields per D-15."""
    from dataclasses import FrozenInstanceError

    from tpwalk.models import ScrapeStats

    stats = ScrapeStats(
        pass1_urls=100,
        pass2_urls=50,
        raw_count=150,
        unique_count=120,
        regions_scraped=57,
        regions_failed=3,
    )
    assert stats.pass1_urls == 100
    assert stats.pass2_urls == 50
    assert stats.raw_count == 150
    assert stats.unique_count == 120
    assert stats.regions_scraped == 57
    assert stats.regions_failed == 3

    # Verify frozen
    try:
        stats.pass1_urls = 999  # type: ignore[misc]
        msg = "ScrapeStats should be frozen"
        raise AssertionError(msg)
    except FrozenInstanceError:
        pass  # expected


# --- Tests for _parse_location_picker ---


def test_parse_location_picker_extracts_region_codes() -> None:
    """Location picker HTML yields region codes from href patterns like /us/, /de/ (D-01, D-02)."""
    from tpwalk.scrape._regional import _parse_location_picker

    html = '<html><body><div class="location-picker"><a href="/us/">United States</a><a href="/de/">Germany</a><a href="/uk/">United Kingdom</a><a href="/fr/">France</a><a href="/cac/">Canada</a></div></body></html>'
    result = _parse_location_picker(html)
    assert "us" in result
    assert "de" in result
    assert "uk" in result
    assert "fr" in result
    assert "cac" in result


def test_parse_location_picker_empty_html() -> None:
    """Empty or malformed HTML returns empty set."""
    from tpwalk.scrape._regional import _parse_location_picker

    assert _parse_location_picker("") == set()
    assert _parse_location_picker("<html></html>") == set()
    assert _parse_location_picker("<div>no links here</div>") == set()


# --- Tests for HARDCODED_REGIONS ---


def test_hardcoded_regions_is_frozenset_of_38() -> None:
    """HARDCODED_REGIONS contains exactly 38 known region codes and is a frozenset.

    D-01 lists 38 codes (the CONTEXT.md text says "37" but the enumerated list
    contains 38 entries including cl and co).
    """
    from tpwalk.scrape._regional import HARDCODED_REGIONS

    assert isinstance(HARDCODED_REGIONS, frozenset)
    assert len(HARDCODED_REGIONS) == 38
    # Spot-check known codes from D-01
    for code in ("us", "uk", "de", "fr", "au", "nz", "be", "jp", "kr", "br", "cl", "co"):
        assert code in HARDCODED_REGIONS, f"Missing expected region: {code}"


# --- Tests for discover_regions ---

# Brute-force discovery is a CONTENT gate, not a status gate: the live origin
# soft-404s every region code to HTTP 200, so a populated `var productTree = {...}`
# is the only reliable liveness signal. Mock "live" regions must therefore serve a
# body with a populated productTree, mirroring a real GPL page.
_VALID_GPL_BODY = '<html><body><script>var productTree = {"1": [{"href": "https://static.tp-link.com/x.tgz", "app_folder": "us", "model_name": "X"}]};</script></body></html>'


def _make_region_transport(
    *,
    live_regions: set[str],
    picker_html: str = "",
    picker_status: int = 200,
) -> httpx.MockTransport:
    """Build a MockTransport for region discovery tests.

    Returns a populated-productTree page for GPL pages of live_regions, picker_html
    for the picker URL, and 404 for everything else.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Location picker
        if "choose-your-location" in url:
            return httpx.Response(picker_status, text=picker_html)
        # GPL page content checks -- a live region serves a populated productTree
        if "/support/gpl-code/" in url:
            for region in live_regions:
                if f"/{region}/support/gpl-code/" in url:
                    return httpx.Response(200, text=_VALID_GPL_BODY)
            return httpx.Response(404)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_discover_regions_unions_all_sources() -> None:
    """discover_regions returns union of brute-force hits, picker codes, and hardcoded (D-01)."""
    from tpwalk.scrape._regional import HARDCODED_REGIONS, discover_regions

    picker_html = '<a href="/za/">South Africa</a><a href="/eg/">Egypt</a>'
    # "za" and "eg" are picker-only, "xx" is a brute-force hit not in picker or hardcoded
    transport = _make_region_transport(
        live_regions={"xx", "us", "de", "za", "eg"},
        picker_html=picker_html,
    )
    client = httpx.AsyncClient(transport=transport)
    sem = asyncio.Semaphore(50)

    async def _run() -> frozenset[str]:
        async with client:
            return await discover_regions(client=client, sem=sem)

    result = asyncio.run(_run())
    assert isinstance(result, frozenset)
    # Must include hardcoded regions
    assert HARDCODED_REGIONS.issubset(result)
    # Must include picker results
    assert "za" in result
    assert "eg" in result
    # Must include brute-force hit
    assert "xx" in result


def test_discover_regions_brute_force_finds_live_codes() -> None:
    """Brute-force pass GETs all aa-zz codes, includes any whose page has a populated productTree (D-01 source 1)."""
    from tpwalk.scrape._regional import discover_regions

    # Only "ab" and "xy" return 200 from brute-force
    transport = _make_region_transport(live_regions={"ab", "xy"}, picker_status=500)
    client = httpx.AsyncClient(transport=transport)
    sem = asyncio.Semaphore(50)

    async def _run() -> frozenset[str]:
        async with client:
            return await discover_regions(client=client, sem=sem)

    result = asyncio.run(_run())
    assert "ab" in result
    assert "xy" in result


def test_discover_regions_fallback_when_picker_and_brute_fail() -> None:
    """discover_regions returns at least 38 hardcoded regions even when picker and brute-force both fail (D-02)."""
    from tpwalk.scrape._regional import HARDCODED_REGIONS, discover_regions

    # Picker returns 500, brute-force finds nothing (all 404)
    transport = _make_region_transport(live_regions=set(), picker_status=500)
    client = httpx.AsyncClient(transport=transport)
    sem = asyncio.Semaphore(50)

    async def _run() -> frozenset[str]:
        async with client:
            return await discover_regions(client=client, sem=sem)

    result = asyncio.run(_run())
    assert HARDCODED_REGIONS.issubset(result), "Hardcoded regions must always be present"
    assert len(result) >= 38


# --- Tests for RegionalSource two-pass crawler ---

# Helper HTML fixtures for region pages with productTree

_REGION_PAGE_DIRECT = """<html><head></head><body><script>
var productTree = {{
    "1": [
        {{"href": "https://static.tp-link.com/upload/gpl-code/2024/archive_a.tar.gz", "app_folder": "{region}", "model_name": "Archer AX50"}},
        {{"href": "https://static.tp-link.com/resources/gpl/old_b.tgz", "app_folder": "{region}", "model_name": "TL-WR740N"}}
    ]
}};
</script></body></html>"""

_REGION_PAGE_PHPPAGE = """<html><head></head><body><script>
var productTree = {{
    "1": [
        {{"href": "?model=Deco BE95", "app_folder": "{region}", "model_name": "Deco BE95"}},
        {{"href": "?model=Archer AX21", "app_folder": "{region}", "model_name": "Archer AX21"}}
    ]
}};
</script></body></html>"""

_REGION_PAGE_MIXED = """<html><head></head><body><script>
var productTree = {{
    "1": [
        {{"href": "https://static.tp-link.com/upload/gpl-code/2024/direct.tar.gz", "app_folder": "{region}", "model_name": "Archer AX50"}},
        {{"href": "?model=Deco BE95", "app_folder": "{region}", "model_name": "Deco BE95"}}
    ]
}};
</script></body></html>"""

_REGION_PAGE_EMPTY_TREE = """<html><head></head><body><script>
var productTree = [];
</script>
<a href="https://static.tp-link.com/resources/gpl/acs_server.tar.gz">GPL</a>
</body></html>"""

_PHPPAGE_FRAGMENT = '<table class="list"><tr><td><a href="https://static.tp-link.com/upload/gpl-code/2024/202401/20240115/{model}_GPL.tar.gz">Download</a></td></tr></table>'

_MINIMAL_PICKER = '<html><body><a href="/us/">US</a><a href="/de/">DE</a><a href="/uk/">UK</a></body></html>'


def _match_region_in_url(url: str, regions: dict[str, object]) -> str | None:
    """Find the first region code whose GPL page URL fragment appears in *url*."""
    for region in regions:
        if f"/{region}/support/gpl-code/" in url:
            return region
    return None


def _handle_gpl_page(
    url: str,
    *,
    regions_html: dict[str, str],
    fail_regions: dict[str, int],
    retries: dict[str, list[int]],
) -> httpx.Response:
    """Route a GPL page request to the correct mock response."""
    retry_region = _match_region_in_url(url, retries)
    if retry_region is not None:
        statuses = retries[retry_region]
        return httpx.Response(statuses.pop(0) if statuses else 404)

    fail_region = _match_region_in_url(url, fail_regions)
    if fail_region is not None:
        return httpx.Response(fail_regions[fail_region])

    html_region = _match_region_in_url(url, regions_html)
    if html_region is not None:
        return httpx.Response(200, text=regions_html[html_region])

    return httpx.Response(404)


def _handle_phppage(
    request: httpx.Request,
    url: str,
    *,
    phppage: dict[str, str],
    check_referer: bool,
) -> httpx.Response:
    """Route a phppage request to the correct mock response."""
    if check_referer:
        referer = request.headers.get("referer", "")
        if not referer or "support/gpl-code" not in referer:
            return httpx.Response(403)
    for model, fragment in phppage.items():
        if f"model={model}" in url or f"model={model.replace(' ', '+')}" in url:
            return httpx.Response(200, text=fragment)
    return httpx.Response(200, text="")


@dataclasses.dataclass
class _TransportConfig:
    """Configuration for the RegionalSource mock transport."""

    regions_html: dict[str, str] = dataclasses.field(default_factory=dict)
    phppage_responses: dict[str, str] = dataclasses.field(default_factory=dict)
    picker_html: str = _MINIMAL_PICKER
    fail_regions: dict[str, int] = dataclasses.field(default_factory=dict)
    fail_regions_retry: dict[str, list[int]] = dataclasses.field(default_factory=dict)
    check_referer: bool = False


def _make_regional_transport(cfg: _TransportConfig) -> httpx.MockTransport:
    """Build a MockTransport for RegionalSource integration tests.

    Routes requests based on URL path:
    - /en/choose-your-location/ -> picker_html
    - /{region}/support/gpl-code/ -> regions_html[region] or 404
    - /phppage/gpl-res-list.html -> phppage_responses matched by model param
    - All other URLs return 404.
    """
    retries: dict[str, list[int]] = {k: list(v) for k, v in cfg.fail_regions_retry.items()}
    request_log: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append((request.method, url))

        if "choose-your-location" in url:
            return httpx.Response(200, text=cfg.picker_html)
        if "/support/gpl-code/" in url:
            return _handle_gpl_page(url, regions_html=cfg.regions_html, fail_regions=cfg.fail_regions, retries=retries)
        if "phppage/gpl-res-list.html" in url:
            return _handle_phppage(request, url, phppage=cfg.phppage_responses, check_referer=cfg.check_referer)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    transport._request_log = request_log  # type: ignore[attr-defined]
    return transport


def test_regional_source_name() -> None:
    """RegionalSource.name is 'regional_crawl' (D-14)."""
    from tpwalk.scrape._regional import RegionalSource

    source = RegionalSource()
    assert source.name == "regional_crawl"


def test_regional_source_two_pass_with_direct_and_phppage() -> None:
    """RegionalSource.run() returns URLs from Pass 1 (direct) and Pass 2 (phppage).

    Sets up 3 regions: one with direct URLs, one with phppage items, one with mixed.
    The phppage items should be followed in Pass 2 and resolved to URLs.
    """
    from tpwalk.scrape._regional import RegionalSource

    regions_html = {
        "us": _REGION_PAGE_DIRECT.format(region="us"),
        "de": _REGION_PAGE_PHPPAGE.format(region="de"),
        "uk": _REGION_PAGE_MIXED.format(region="uk"),
    }
    phppage_responses = {
        "Deco BE95": _PHPPAGE_FRAGMENT.format(model="Deco_BE95"),
        "Archer AX21": _PHPPAGE_FRAGMENT.format(model="Archer_AX21"),
    }
    transport = _make_regional_transport(
        _TransportConfig(
            regions_html=regions_html,
            phppage_responses=phppage_responses,
        )
    )

    source = RegionalSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Direct URLs from us and uk regions
    assert "https://static.tp-link.com/upload/gpl-code/2024/archive_a.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/old_b.tgz" in result
    assert "https://static.tp-link.com/upload/gpl-code/2024/direct.tar.gz" in result
    # phppage URLs from de (both models) and uk (Deco BE95)
    assert any("Deco_BE95_GPL.tar.gz" in u for u in result)
    assert any("Archer_AX21_GPL.tar.gz" in u for u in result)
    # Should have URLs from all 3 regions
    assert len(result) >= 5


def test_regional_source_failing_region_skipped() -> None:
    """A region returning 500 is retried once then skipped. Good regions still produce URLs (D-03, SCRP-05, Pitfall 2)."""
    from tpwalk.scrape._regional import RegionalSource

    regions_html = {
        "us": _REGION_PAGE_DIRECT.format(region="us"),
        "de": _REGION_PAGE_DIRECT.format(region="de"),
    }
    transport = _make_regional_transport(
        _TransportConfig(
            regions_html=regions_html,
            fail_regions={"fail": 500},
            picker_html='<a href="/us/">US</a><a href="/de/">DE</a><a href="/fail/">FAIL</a>',
        )
    )

    source = RegionalSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # us and de URLs should be present despite fail region error
    assert "https://static.tp-link.com/upload/gpl-code/2024/archive_a.tar.gz" in result
    assert "https://static.tp-link.com/resources/gpl/old_b.tgz" in result
    assert len(result) >= 2


def test_regional_source_retry_then_skip() -> None:
    """Region returning 500 gets exactly one retry (D-03). Transport log shows 2 requests to the failing URL."""
    from tpwalk.scrape._regional import RegionalSource

    regions_html = {
        "us": _REGION_PAGE_DIRECT.format(region="us"),
    }
    # Use a 3-letter failing code ("zzz") so it is sourced ONLY from the picker:
    # the brute-force sweep is bounded to two-letter aa-zz codes, so it never
    # touches "zzz" and the request log isolates Pass 1's single retry cleanly.
    transport = _make_regional_transport(
        _TransportConfig(
            regions_html=regions_html,
            fail_regions_retry={"zzz": [500, 500]},
            picker_html='<a href="/us/">US</a><a href="/zzz/">ZZZ</a>',
        )
    )

    source = RegionalSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # us URLs still present
    assert len(result) >= 2

    # Check transport request log: "zzz" region should have exactly 2 GET requests
    # (1 initial + 1 retry in Pass 1; brute-force never requests this 3-letter code).
    log = transport._request_log  # type: ignore[attr-defined]
    zzz_get_requests = [(m, u) for m, u in log if "/zzz/support/gpl-code/" in u and m == "GET"]
    assert len(zzz_get_requests) == 2, f"Expected 2 GET requests to failing region zzz (1 initial + 1 retry), got {len(zzz_get_requests)}: {zzz_get_requests}"


def test_regional_source_empty_tree_falls_back_to_html() -> None:
    """Regions with empty productTree [] still return URLs from HTML fallback (D-06)."""
    from tpwalk.scrape._regional import RegionalSource

    regions_html = {
        "be": _REGION_PAGE_EMPTY_TREE,
    }
    transport = _make_regional_transport(
        _TransportConfig(
            regions_html=regions_html,
            picker_html='<a href="/be/">BE</a>',
        )
    )

    source = RegionalSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert "https://static.tp-link.com/resources/gpl/acs_server.tar.gz" in result


def test_regional_source_phppage_referer_header() -> None:
    """phppage requests include Referer header matching the region GPL page URL (Pitfall 4).

    Transport returns 403 if Referer is missing or incorrect; 200 if correct.
    """
    from tpwalk.scrape._regional import RegionalSource

    regions_html = {
        "us": _REGION_PAGE_PHPPAGE.format(region="us"),
    }
    phppage_responses = {
        "Deco BE95": _PHPPAGE_FRAGMENT.format(model="Deco_BE95"),
        "Archer AX21": _PHPPAGE_FRAGMENT.format(model="Archer_AX21"),
    }
    transport = _make_regional_transport(
        _TransportConfig(
            regions_html=regions_html,
            phppage_responses=phppage_responses,
            picker_html='<a href="/us/">US</a>',
            check_referer=True,
        )
    )

    source = RegionalSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    # If Referer was missing/wrong, phppage would have returned 403 -> no phppage URLs
    assert any("Deco_BE95_GPL.tar.gz" in u for u in result), f"phppage URLs missing (Referer header check failed): {result}"


def test_regional_source_pass1_and_pass2_distinct() -> None:
    """Pass 1 uses region page fetches; Pass 2 uses phppage fetches.

    Verified by inspecting transport request log: region page URLs should appear
    before phppage URLs.
    """
    from tpwalk.scrape._regional import RegionalSource

    regions_html = {
        "us": _REGION_PAGE_MIXED.format(region="us"),
    }
    phppage_responses = {
        "Deco BE95": _PHPPAGE_FRAGMENT.format(model="Deco_BE95"),
    }
    transport = _make_regional_transport(
        _TransportConfig(
            regions_html=regions_html,
            phppage_responses=phppage_responses,
            picker_html='<a href="/us/">US</a>',
        )
    )

    source = RegionalSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    log = transport._request_log  # type: ignore[attr-defined]
    # Both region page and phppage requests should be in the log
    region_requests = [(m, u) for m, u in log if "/support/gpl-code/" in u]
    phppage_requests = [(m, u) for m, u in log if "phppage/gpl-res-list" in u]
    assert len(region_requests) > 0, "No region page requests found"
    assert len(phppage_requests) > 0, "No phppage requests found"

    # Direct and phppage URLs should both be in result
    assert "https://static.tp-link.com/upload/gpl-code/2024/direct.tar.gz" in result
    assert any("Deco_BE95_GPL.tar.gz" in u for u in result)


def test_region_error_does_not_suppress_subsequent_regions() -> None:
    """Region error at index N does not suppress output from regions N+1..end.

    This is the critical return-vs-continue guard from RESEARCH.md Pitfall 2.
    A failing region (returning 500) in the middle of the list must not prevent
    subsequent good regions from producing URLs.
    """
    from tpwalk.scrape._regional import RegionalSource

    # good1, fail, good2 -- fail is between two good regions
    regions_html = {
        "us": _REGION_PAGE_DIRECT.format(region="us"),
        "uk": _REGION_PAGE_DIRECT.format(region="uk"),
    }
    transport = _make_regional_transport(
        _TransportConfig(
            regions_html=regions_html,
            fail_regions={"de": 500},
            picker_html='<a href="/us/">US</a><a href="/de/">DE</a><a href="/uk/">UK</a>',
        )
    )

    source = RegionalSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Both good regions must produce URLs
    us_urls = [u for u in result if "archive_a" in u or "old_b" in u]
    assert len(us_urls) >= 2, f"Expected URLs from good regions, got: {result}"
    # The critical assertion: URLs exist from both regions despite the failure in between
    assert len(result) >= 2, f"Expected at least 2 URLs from the 2 good regions, got {len(result)}: {result}"
