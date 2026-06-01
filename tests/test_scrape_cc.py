"""Tests for tpwalk.scrape._common_crawl -- Common Crawl index discovery, page-based pagination, retry, no-filtering.

Tests use inline JSON/NDJSON fixtures and httpx.MockTransport so no real network requests are made.
Per SCRP-11, SCRP-12, D-05, D-06, D-07, D-08.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx

# --- Canned collinfo.json fixtures ---


def _make_index_entry(index_id: str) -> dict[str, str]:
    """Build one collinfo.json index entry with the standard fields."""
    return {
        "id": index_id,
        "name": index_id,
        "cdx-api": f"https://index.commoncrawl.org/{index_id}-index",
    }


# --- Canned NDJSON response fixtures ---


def _make_ndjson(*urls: str) -> str:
    """Build a Common Crawl NDJSON response body from URL strings."""
    lines = []
    for url in urls:
        record = {
            "urlkey": "com,tp-link,static)/some/path",
            "timestamp": "20240101000000",
            "url": url,
            "mime": "application/gzip",
            "status": "200",
            "digest": "SHA1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "length": "12345",
            "offset": "0",
            "filename": "crawl-data/CC-MAIN-2024-01/segments/0/warc/00000.warc.gz",
        }
        lines.append(json.dumps(record))
    return "\n".join(lines)


def _show_num_pages(n: int) -> str:
    """Build a showNumPages response with the given page count."""
    return json.dumps({"pages": n, "pageSize": 5, "blocks": n})


# --- Helpers ---


def _extract_url_param(request_url: str) -> str:
    """Extract the 'url' query parameter from a CC CDX request URL."""
    parsed = urlparse(request_url)
    params = parse_qs(parsed.query)
    return params.get("url", [""])[0]


def _get_param(request_url: str, key: str) -> str:
    """Get a single query parameter value from a URL."""
    parsed = urlparse(request_url)
    params = parse_qs(parsed.query)
    return params.get(key, [""])[0]


# --- Tests ---


def test_cc_source_name() -> None:
    """CommonCrawlSource.name is 'common_crawl' (D-14)."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    source = CommonCrawlSource()
    assert source.name == "common_crawl"


def test_cc_discovers_indices_from_collinfo() -> None:
    """Mock transport serves collinfo.json with 3 fake indices. Assert CommonCrawlSource queries all 3 index endpoints."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    indices = [_make_index_entry(f"CC-MAIN-2024-0{i}") for i in range(1, 4)]
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=indices)

        # For each index, showNumPages returns 1 page
        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(1))

        # For each index page request, return one NDJSON record
        for idx in indices:
            if idx["id"] in url and "showNumPages" not in url:
                return httpx.Response(200, text=_make_ndjson(f"https://static.tp-link.com/gpl/{idx['id']}.tar.gz"))

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    # All 3 index endpoints should have been queried (excluding collinfo.json itself)
    index_requests = [u for u in request_log if "index" in u and "collinfo.json" not in u]
    queried_ids = set()
    for req_url in index_requests:
        for idx in indices:
            if idx["id"] in req_url:
                queried_ids.add(idx["id"])
    assert len(queried_ids) == 3, f"Expected 3 indices queried, got {len(queried_ids)}: {queried_ids}"


def test_cc_queries_all_indices() -> None:
    """Mock transport serves collinfo.json with 5 indices. All 5 return valid NDJSON with distinct URLs. Assert result set contains URLs from all 5 (D-06)."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    indices = [_make_index_entry(f"CC-MAIN-2024-{i:02d}") for i in range(1, 6)]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=indices)

        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(1))

        for idx in indices:
            if idx["id"] in url and "showNumPages" not in url:
                return httpx.Response(200, text=_make_ndjson(f"https://static.tp-link.com/gpl/{idx['id']}.tar.gz"))

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Every index should have contributed one unique URL
    for idx in indices:
        expected_url = f"https://static.tp-link.com/gpl/{idx['id']}.tar.gz"
        assert expected_url in result, f"Missing URL from index {idx['id']}: {expected_url}"


def test_cc_page_based_pagination() -> None:
    """One index where showNumPages returns 3. Pages 0, 1, 2 each return distinct NDJSON records. Assert all 3 pages' URLs present."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    index = _make_index_entry("CC-MAIN-2024-01")

    page_data = {
        "0": _make_ndjson("https://static.tp-link.com/gpl/page0.tar.gz"),
        "1": _make_ndjson("https://static.tp-link.com/gpl/page1.tar.gz"),
        "2": _make_ndjson("https://static.tp-link.com/gpl/page2.tar.gz"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=[index])

        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(3))

        if index["id"] in url and "showNumPages" not in url:
            page = _get_param(url, "page")
            if page in page_data:
                return httpx.Response(200, text=page_data[page])
            return httpx.Response(200, text="")

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    assert "https://static.tp-link.com/gpl/page0.tar.gz" in result
    assert "https://static.tp-link.com/gpl/page1.tar.gz" in result
    assert "https://static.tp-link.com/gpl/page2.tar.gz" in result


def test_cc_url_extraction() -> None:
    """NDJSON records with many fields -- only the 'url' field values are extracted (SCRP-12)."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    index = _make_index_entry("CC-MAIN-2024-01")
    ndjson_body = _make_ndjson("https://static.tp-link.com/gpl/archive.tar.gz")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=[index])
        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(1))
        if index["id"] in url:
            return httpx.Response(200, text=ndjson_body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Should contain the URL, not timestamps or digests
    assert "https://static.tp-link.com/gpl/archive.tar.gz" in result
    # Should not contain non-URL strings from the NDJSON record
    for item in result:
        assert item.startswith("http"), f"Non-URL string in result: {item}"


def test_cc_404_is_no_captures() -> None:
    """HTTP 404 from CC for one index means no captures -- not an error. URLs from other index are present."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    idx_good = _make_index_entry("CC-MAIN-2024-01")
    idx_404 = _make_index_entry("CC-MAIN-2024-02")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=[idx_good, idx_404])

        if idx_404["id"] in url:
            return httpx.Response(404, json={"message": "No Captures found for: static.tp-link.com"})

        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(1))

        if idx_good["id"] in url:
            return httpx.Response(200, text=_make_ndjson("https://static.tp-link.com/gpl/good.tar.gz"))

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    assert "https://static.tp-link.com/gpl/good.tar.gz" in result


def test_cc_index_failure_continues(monkeypatch: object) -> None:
    """503 for one index on all retries, 200 for others. Working indices contribute URLs; failing index is skipped (D-07)."""
    from unittest.mock import AsyncMock

    from tpwalk.scrape import _common_crawl as cc_mod
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    monkeypatch.setattr(cc_mod.asyncio, "sleep", AsyncMock())  # type: ignore[attr-defined]

    idx_good = _make_index_entry("CC-MAIN-2024-01")
    idx_fail = _make_index_entry("CC-MAIN-2024-02")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=[idx_good, idx_fail])

        if idx_fail["id"] in url:
            return httpx.Response(503)

        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(1))

        if idx_good["id"] in url:
            return httpx.Response(200, text=_make_ndjson("https://static.tp-link.com/gpl/good.tar.gz"))

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # Good index URLs should be present despite failing index
    assert "https://static.tp-link.com/gpl/good.tar.gz" in result


def test_cc_retry_exponential_backoff(monkeypatch: object) -> None:
    """503 twice then 200 for one index. Track request count -- assert 3 total requests (2 retries + 1 success)."""
    from unittest.mock import AsyncMock

    from tpwalk.scrape import _common_crawl as cc_mod
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    mock_sleep = AsyncMock()
    monkeypatch.setattr(cc_mod.asyncio, "sleep", mock_sleep)  # type: ignore[attr-defined]

    index = _make_index_entry("CC-MAIN-2024-01")
    request_log: list[str] = []
    # Track per-prefix call counts so we only fail the TP-Link prefix
    tp_link_call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tp_link_call_count
        url = str(request.url)
        request_log.append(url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=[index])

        url_param = _extract_url_param(url)

        # showNumPages for TP-Link prefix: fail twice then succeed
        if "showNumPages" in url and index["id"] in url and url_param == "static.tp-link.com/*":
            tp_link_call_count += 1
            if tp_link_call_count <= 2:
                return httpx.Response(503)
            return httpx.Response(200, text=_show_num_pages(1))

        # showNumPages for Mercusys: always succeed with 0 pages
        if "showNumPages" in url and index["id"] in url:
            return httpx.Response(200, text=_show_num_pages(0))

        if index["id"] in url and "showNumPages" not in url:
            return httpx.Response(200, text=_make_ndjson("https://static.tp-link.com/gpl/retried.tar.gz"))

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    # After retry succeeds, should get the URL
    assert "https://static.tp-link.com/gpl/retried.tar.gz" in result

    # Should have made 3 requests to the TP-Link showNumPages endpoint (2 failures + 1 success)
    tp_link_show_requests = [u for u in request_log if "showNumPages" in u and index["id"] in u and _extract_url_param(u) == "static.tp-link.com/*"]
    assert len(tp_link_show_requests) == 3, f"Expected 3 TP-Link showNumPages requests, got {len(tp_link_show_requests)}: {tp_link_show_requests}"

    # Verify exponential backoff sleep calls
    sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
    assert 2.0 in sleep_args, f"Expected 2.0s backoff, got: {sleep_args}"
    assert 4.0 in sleep_args, f"Expected 4.0s backoff, got: {sleep_args}"


def test_cc_queries_both_hosts() -> None:
    """Assert queries include both 'static.tp-link.com/*' and 'static.mercusys.com/gpl/*' per D-01, D-02."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    index = _make_index_entry("CC-MAIN-2024-01")
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        request_log.append(url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=[index])
        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(0))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    asyncio.run(_run())

    # Extract url= params from requests to the index endpoint
    queried_url_params = set()
    for req_url in request_log:
        if index["id"] in req_url:
            param = _extract_url_param(req_url)
            if param:
                queried_url_params.add(param)

    assert "static.tp-link.com/*" in queried_url_params, f"Missing static.tp-link.com/* query, got: {queried_url_params}"
    assert "static.mercusys.com/gpl/*" in queried_url_params, f"Missing static.mercusys.com/gpl/* query, got: {queried_url_params}"


def test_cc_no_url_filtering() -> None:
    """NDJSON with HTML, CSS, image, and archive URLs -- ALL in result set (D-08)."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    index = _make_index_entry("CC-MAIN-2024-01")
    mixed_urls = [
        "https://static.tp-link.com/gpl/archive.tar.gz",
        "https://static.tp-link.com/css/style.css",
        "https://static.tp-link.com/images/product.jpg",
        "https://static.tp-link.com/en/some-page.html",
    ]
    ndjson_body = _make_ndjson(*mixed_urls)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "collinfo.json" in url:
            return httpx.Response(200, json=[index])
        if "showNumPages" in url:
            return httpx.Response(200, text=_show_num_pages(1))
        if index["id"] in url:
            return httpx.Response(200, text=ndjson_body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())

    for expected_url in mixed_urls:
        assert expected_url in result, f"Missing URL (D-08 no-filtering): {expected_url}"


def test_cc_empty_collinfo() -> None:
    """collinfo.json returns empty array []. Assert empty set returned, no crash."""
    from tpwalk.scrape._common_crawl import CommonCrawlSource

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "collinfo.json" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = CommonCrawlSource()
    client = httpx.AsyncClient(transport=transport)

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert result == set()
