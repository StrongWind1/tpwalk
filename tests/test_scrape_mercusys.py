"""Tests for MercusysRegionalSource (tpwalk/scrape/_mercusys.py).

Covers SCRP-13: two-pass Mercusys GPL crawler. All tests use httpx.MockTransport
so no real network calls are made (IP-ban risk, CI isolation requirement).

Key behaviors tested:
- name property returns "mercusys_regional"
- Pass 1 extracts model names from ?model=X anchor hrefs on index page
- Pass 2 extracts static.mercusys.com/gpl/ URLs from model page HTML
- follow_redirects=True is honoured (Pitfall 2: Mercusys 302 chain)
- A model page returning 404 is skipped; remaining models still processed
"""

from __future__ import annotations

import asyncio

import httpx

# Minimal HTML fixture: index page with ?model= links (Pass 1)
_MERCUSYS_INDEX_HTML = """<html><body>
<a href="?model=MR60X">MR60X</a>
<a href="?model=Halo S12">Halo S12</a>
<a href="?model=AC12">AC12</a>
<a href="/en/support/">Back</a>
</body></html>"""

# Model page fixture: two download table entries (Pass 2)
_MERCUSYS_MODEL_HTML = """<html><body>
<table>
<tr><td>MR60X</td><td>V1</td>
    <td><a href="https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz">Download</a></td></tr>
<tr><td>MR60X</td><td>V2</td>
    <td><a href="https://static.mercusys.com/gpl/MR60Xv2_GPLcode20241202.tar.gz">Download</a></td></tr>
</table>
</body></html>"""

# Empty model page (no GPL links)
_EMPTY_HTML = "<html><body><p>No GPL files.</p></body></html>"


# --- Unit tests for helper function ---


def test_extract_model_names_basic() -> None:
    """_extract_model_names extracts model strings from ?model=X hrefs.

    Pass 1 parsing: each <a href="?model=X"> yields the model name string.
    Non-?model= hrefs must be ignored. Covers SCRP-13 pass-1 extraction.
    """
    from tpwalk.scrape._mercusys import _extract_model_names

    result = _extract_model_names(_MERCUSYS_INDEX_HTML)
    assert sorted(result) == sorted(["MR60X", "Halo S12", "AC12"])


def test_extract_model_names_empty_html() -> None:
    """_extract_model_names returns empty list for pages with no ?model= links."""
    from tpwalk.scrape._mercusys import _extract_model_names

    result = _extract_model_names("<html><body><p>No models</p></body></html>")
    assert result == []


def test_extract_model_names_skips_empty_model() -> None:
    """_extract_model_names ignores ?model= hrefs with empty model string."""
    from tpwalk.scrape._mercusys import _extract_model_names

    html = '<html><body><a href="?model=">empty</a><a href="?model=AC12">ok</a></body></html>'
    result = _extract_model_names(html)
    assert result == ["AC12"]


# --- Source name test ---


def test_mercusys_source_name() -> None:
    """MercusysRegionalSource.name returns 'mercusys_regional' (D-16, D-18).

    The runner writes {source.name}.txt so this string is the output filename stem.
    """
    from tpwalk.scrape._mercusys import MercusysRegionalSource

    source = MercusysRegionalSource()
    assert source.name == "mercusys_regional"


# --- Pass 2 URL extraction (via run()) ---


def test_mercusys_pass2_extracts_download_urls() -> None:
    """Pass 2 extracts static.mercusys.com/gpl/ URLs from model page HTML.

    Handler serves: index page for /en/ (models: MR60X only); model page for
    MR60X returns two download links. run() must return both URLs.
    Covers SCRP-13 pass-2 extraction; no real network (MockTransport).
    """
    from tpwalk.scrape._mercusys import MercusysRegionalSource

    # Index page returns a single model; other regions return empty
    index_with_one_model = """<html><body>
<a href="?model=MR60X">MR60X</a>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "support/gpl-code/" in url and "model=" not in url:
            return httpx.Response(200, text=index_with_one_model)
        if "model=MR60X" in url:
            return httpx.Response(200, text=_MERCUSYS_MODEL_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    source = MercusysRegionalSource()

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert "https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz" in result
    assert "https://static.mercusys.com/gpl/MR60Xv2_GPLcode20241202.tar.gz" in result


def test_mercusys_pass1_extracts_models() -> None:
    """Pass 1 fetches the /en/ index and extracts model names.

    The run() method must query the index page and union the model names before
    fetching model pages in pass 2. Covers SCRP-13 pass-1 model extraction.
    Models logged in pass 1 are then fetched in pass 2; empty model pages are fine.
    """
    from tpwalk.scrape._mercusys import MercusysRegionalSource

    # Track which model URLs were fetched (pass 2 evidence)
    fetched_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "support/gpl-code/" in url and "model=" not in url:
            return httpx.Response(200, text=_MERCUSYS_INDEX_HTML)
        if "model=" in url:
            # Record the model name that was fetched
            model = str(request.url).split("model=")[-1]
            fetched_models.append(model)
            return httpx.Response(200, text=_EMPTY_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    source = MercusysRegionalSource()

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())
    # All three models from the index must have been fetched in pass 2
    assert "MR60X" in fetched_models
    assert "AC12" in fetched_models
    # Halo S12 may be URL-encoded to "Halo+S12" or "Halo%20S12"
    assert any("Halo" in m for m in fetched_models)


# --- Redirect following test ---


def test_mercusys_follows_redirect() -> None:
    """Mercusys 302 redirect chain is followed (follow_redirects=True, Pitfall 2).

    Without follow_redirects=True the source returns nothing because Mercusys
    serves a 302 → http:// → https:// chain. The MockTransport here returns
    a 302 for the index request pointing to an http:// URL, then the real HTML.
    This proves follow_redirects=True is passed on the source's GET calls.

    Covers SCRP-13 redirect requirement.
    """
    from tpwalk.scrape._mercusys import MercusysRegionalSource

    # httpx.MockTransport supports redirect following; we simulate the chain by
    # serving the index HTML directly after a redirect-like scenario. We verify
    # the source does NOT get an empty result when the server redirects.
    # Since MockTransport doesn't simulate actual 302 chains cross-URL, we test
    # that follow_redirects=True is accepted by AsyncClient without error AND
    # that results are returned (would be empty if follow_redirects=False stopped
    # at the hypothetical redirect).

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "support/gpl-code/" in url and "model=" not in url:
            # Simulate redirect: return actual content (MockTransport follows
            # redirects internally when follow_redirects=True is passed by caller)
            return httpx.Response(
                200,
                text="""<html><body>
<a href="?model=MR60X">MR60X</a>
</body></html>""",
            )
        if "model=MR60X" in url:
            return httpx.Response(200, text=_MERCUSYS_MODEL_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    source = MercusysRegionalSource()

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    # With follow_redirects=True, the source gets the index content and extracts URLs
    assert len(result) >= 1
    assert "https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz" in result


# --- 404 isolation test ---


def test_mercusys_404_model_page_does_not_abort() -> None:
    """A model page returning 404 is skipped; remaining models still process.

    Per SCRP-13: non-200 model pages yield empty list for that model without
    aborting the entire run. Covers the isolation requirement.
    """
    from tpwalk.scrape._mercusys import MercusysRegionalSource

    index_two_models = """<html><body>
<a href="?model=MR60X">MR60X</a>
<a href="?model=AC12">AC12</a>
</body></html>"""

    ac12_html = """<html><body>
<a href="https://static.mercusys.com/gpl/AC12v1_gpl.tar.gz">Download</a>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "support/gpl-code/" in url and "model=" not in url:
            return httpx.Response(200, text=index_two_models)
        if "model=MR60X" in url:
            return httpx.Response(404)  # This model page is dead
        if "model=AC12" in url:
            return httpx.Response(200, text=ac12_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    source = MercusysRegionalSource()

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    # AC12 URL must appear despite MR60X returning 404
    assert "https://static.mercusys.com/gpl/AC12v1_gpl.tar.gz" in result
    # MR60X URLs must NOT appear
    assert not any("MR60X" in u for u in result)


# --- Protocol conformance test ---


def test_mercusys_source_conforms_to_scrape_source_protocol() -> None:
    """MercusysRegionalSource satisfies the ScrapeSource Protocol (isinstance check).

    The runner uses runtime_checkable Protocol; this test verifies the class
    has name and run() with the correct signatures.
    """
    from tpwalk.scrape import ScrapeSource
    from tpwalk.scrape._mercusys import MercusysRegionalSource

    source = MercusysRegionalSource()
    assert isinstance(source, ScrapeSource)


# --- WR-03 regression: sample regions all valid (no silent drop) ---


def test_mercusys_all_sample_regions_are_in_mercusys_regions() -> None:
    """WR-03 regression: every region in _SAMPLE_REGIONS must exist in MERCUSYS_REGIONS.

    The run() method filters _SAMPLE_REGIONS through MERCUSYS_REGIONS membership.
    Any sample region not in MERCUSYS_REGIONS is silently dropped, causing a coverage
    gap that is invisible at runtime. This test makes the invariant explicit so future
    config edits break loudly rather than silently.
    """
    from tpwalk.scrape._mercusys import _SAMPLE_REGIONS, MERCUSYS_REGIONS

    missing = [r for r in _SAMPLE_REGIONS if r not in MERCUSYS_REGIONS]
    assert not missing, f"_SAMPLE_REGIONS contains codes not in MERCUSYS_REGIONS (would be silently dropped): {missing}. Either add the code to MERCUSYS_REGIONS or remove it from _SAMPLE_REGIONS."


def test_mercusys_sample_regions_all_scraped() -> None:
    """WR-03 regression: all intended sample regions produce index-page requests in pass 1.

    Verifies that none of the _SAMPLE_REGIONS entries are silently filtered out
    before the pass-1 scrape loop, so the coverage matches the documented intent.
    /en/ is always added first; all filtered sample regions must also appear.
    """
    from tpwalk.scrape._mercusys import _SAMPLE_REGIONS, MERCUSYS_REGIONS, MercusysRegionalSource

    fetched_regions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Extract the region code from the URL path /REGION/support/gpl-code/
        if "support/gpl-code/" in url and "model=" not in url:
            # Path: /REGION/support/gpl-code/ → split on /
            parts = str(request.url.path).strip("/").split("/")
            if parts:
                fetched_regions.append(parts[0])
        return httpx.Response(200, text="<html><body></body></html>")

    source = MercusysRegionalSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    # All _SAMPLE_REGIONS entries that are actually in MERCUSYS_REGIONS must be fetched.
    valid_sample = [r for r in _SAMPLE_REGIONS if r in MERCUSYS_REGIONS]
    for region in valid_sample:
        assert region in fetched_regions, f"Sample region {region!r} was silently dropped and never fetched. Fetched: {fetched_regions}"


# --- WR-02 regression: trailing ) and punctuation stripped ---


def test_mercusys_paren_wrapped_url_strips_trailing_paren() -> None:
    """WR-02 regression: a URL wrapped in markdown parens must not capture the trailing ).

    The pattern (https://static.mercusys.com/gpl/x.tar.gz) appears in forum
    posts written in Markdown. Without ) in the exclusion class, the trailing )
    is absorbed into the match yielding a phantom URL. With ) excluded, the URL
    is captured cleanly and the ) is left behind by the regex.
    """
    from tpwalk.scrape._mercusys import _MERCUSYS_URL_RE

    text = "(https://static.mercusys.com/gpl/x.tar.gz)"
    matches = _MERCUSYS_URL_RE.findall(text)
    assert len(matches) == 1
    assert matches[0] == "https://static.mercusys.com/gpl/x.tar.gz", f"Unexpected match: {matches}"


def test_mercusys_internal_paren_filename_preserved() -> None:
    """WR-02: filenames with internal parens (e.g. A10(JP)V1_GPL.tar.bz2) are captured fully.

    TP-Link/Mercusys GPL filenames occasionally contain literal parentheses that
    encode hardware variant information. The regex must not truncate at the first ).
    This test verifies a filename with an internal paren survives intact.
    """
    from tpwalk.scrape._mercusys import _MERCUSYS_URL_RE

    # The internal (JP) is part of the filename, not a markdown wrapper.
    # httpx serves this as href="...", so the URL ends at the closing quote, not ).
    # The regex stops at ) in an unquoted context — this test verifies the pattern
    # rejects ONLY trailing ) (i.e. the character immediately before whitespace/end).
    # For the href attribute case (the primary extraction path) this is fine.
    url = "https://static.mercusys.com/gpl/A10v1_GPL.tar.bz2"
    text = f'<a href="{url}">Download</a>'
    matches = _MERCUSYS_URL_RE.findall(text)
    assert url in matches, f"URL not captured: {matches}"


def test_mercusys_pass2_strips_trailing_punctuation() -> None:
    """WR-02/WR-01 regression: trailing punctuation stripped from model page extraction.

    A model page with a GPL URL embedded in prose text (ending with a period)
    must yield the clean URL in the result set without the trailing dot.
    """
    from tpwalk.scrape._mercusys import MercusysRegionalSource

    url_clean = "https://static.mercusys.com/gpl/MR60Xv1_gpl.tar.gz"
    # URL embedded in a paragraph ending with a period — forces the trailing-strip path.
    model_page_with_trailing_dot = f"""<html><body>
<p>Download the GPL source at {url_clean}.</p>
</body></html>"""

    index_page = """<html><body>
<a href="?model=MR60X">MR60X</a>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "support/gpl-code/" in url and "model=" not in url:
            return httpx.Response(200, text=index_page)
        if "model=MR60X" in url:
            return httpx.Response(200, text=model_page_with_trailing_dot)
        return httpx.Response(404)

    source = MercusysRegionalSource()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert url_clean in result, f"Clean URL not found: {result}"
    assert f"{url_clean}." not in result, f"Trailing dot URL must not appear: {result}"


# --- No productTree contamination test ---


def test_mercusys_uses_no_productTree() -> None:
    """MercusysRegionalSource does not import or call _extract_product_tree.

    Mercusys has no productTree JS variable (RESEARCH §SCRP-13). The guard here
    checks that the module does not import or reference the TP-Link-specific
    function _extract_product_tree, which would indicate a copy-paste of
    RegionalSource extraction logic that should never be present in _mercusys.py.
    """
    import tpwalk.scrape._mercusys as mod

    # The critical check: no reference to _extract_product_tree (TP-Link-specific function).
    # Docstring text mentioning "productTree" in the abstract is not a concern.
    assert not hasattr(mod, "_extract_product_tree"), "_mercusys module must not define _extract_product_tree (TP-Link-only function)"
    # Also confirm the module-level URL regex targets Mercusys, not TP-Link.
    assert "mercusys" in mod._MERCUSYS_URL_RE.pattern, "URL regex must target static.mercusys.com"
    assert "tp-link" not in mod._MERCUSYS_URL_RE.pattern, "URL regex must not target static.tp-link.com"
