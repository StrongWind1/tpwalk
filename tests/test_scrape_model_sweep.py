"""Tests for tpwalk.scrape._model_sweep — name extraction, ?model= harvest, two-phase sweep.

All HTTP is mocked via httpx.MockTransport; regions are injected so discover_regions
is never called. gpl_urls is pointed at a nonexistent path to isolate from the real
ref_gpl_data file and keep the mocked sweep small/fast.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from tpwalk.scrape._model_sweep import ModelSweepSource, _models_from_gpl_url, _model_variants, _normalize_model, load_model_wordlist


def _none(tmp_path: Path) -> Path:
    """A guaranteed-nonexistent path so load_model_wordlist skips GPL parsing."""
    return tmp_path / "no_gpl_urls.txt"


def test_normalize_model_strips_version_region_and_splits_brand() -> None:
    """Firmware/GPL basenames reduce to canonical base model tokens."""
    assert _normalize_model("(B)AP9650v1_1.1.1_[20250804-rel72981]_up_signed_1758134504992.bin") == "AP9650"
    assert _normalize_model("HB810v1_0.3.0_3.0.0_UP_BOOT_2024-05-21_1734333996507.bin") == "HB810"
    assert _normalize_model("TapoC200v5en_1.3.0_up_123456789.bin") == "Tapo C200"
    assert _normalize_model("Archer_C7v5_us-up_signed.bin") == "Archer C7"


def test_model_variants_adds_brand_guess() -> None:
    """Bare series tokens emit brand-prefixed guesses (phppage validates the real one)."""
    assert _model_variants("AX21") == {"AX21", "Archer AX21"}
    assert "Deco X60" in _model_variants("X60")
    assert _model_variants("Archer C7") == {"Archer C7"}


def test_models_from_gpl_url_splits_multi_model() -> None:
    """GPL filenames yield model tokens, splitting multi-model tarballs on underscores."""
    got = _models_from_gpl_url("https://static.tp-link.com/upload/gpl-code/2025/EAP610v4_EAP613v2.tar.gz")
    assert "EAP610" in got
    assert "EAP613" in got
    archer = _models_from_gpl_url("https://static.tp-link.com/resources/gpl/Archer_AX23v1_GPL.tar")
    assert "Archer AX23" in archer


def test_load_model_wordlist_firmware_only(tmp_path: Path) -> None:
    """Wordlist skips folders/non-firmware/noise; GPL source skipped when path is absent."""
    fw = [
        {"key": "firmware/AX21v1_1.0_up_signed_1700000000000.bin", "size": 100, "modified": "x"},
        {"key": "firmware/", "size": 0, "modified": "x"},
        {"key": "app/SomeApp_1.2.3.apk", "size": 50, "modified": "x"},
        {"key": "firmware/1.0.0 Build 140808 Rel.58168n.bin", "size": 70, "modified": "x"},
    ]
    p = tmp_path / "fw.json"
    p.write_text(json.dumps(fw), encoding="utf-8")
    words = load_model_wordlist(p, _none(tmp_path))
    assert {"AX21", "Archer AX21"} <= words
    assert all(not w[0].isdigit() for w in words)


def _sweep_transport(valid_model: str, gpl_url: str) -> httpx.MockTransport:
    """phppage returns gpl_url for valid_model (case-insensitive); gpl-code pages 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "phppage/gpl-res-list" not in url:
            return httpx.Response(404)
        model = parse_qs(urlparse(url).query).get("model", [""])[0].lower()
        if model == valid_model.lower():
            return httpx.Response(200, text=f'<table class="list"><a href="{gpl_url}">Download</a></table>')
        return httpx.Response(200, text="")

    return httpx.MockTransport(handler)


def test_model_sweep_validates_collects_and_records_region(tmp_path: Path) -> None:
    """A matching brand variant validates in probe; its GPL and the answering region are recorded."""
    gpl = "https://static.tp-link.com/upload/gpl-code/2024/AX21_GPL.tar.gz"
    p = tmp_path / "fw.json"
    p.write_text(json.dumps([{"key": "firmware/AX21v1_1.0.0_up_1700000000000.bin", "size": 100, "modified": "x"}]), encoding="utf-8")

    source = ModelSweepSource(firmware_json=p, gpl_urls=_none(tmp_path), regions=["us", "de"], probe_regions=["us"], concurrency=4)
    client = httpx.AsyncClient(transport=_sweep_transport("Archer AX21", gpl))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert gpl in result
    assert source.validated_models == 1
    assert "us" in source.verified_regions  # only the answering region is verified


def test_model_sweep_phase2_expands_region_specific(tmp_path: Path) -> None:
    """Probe validates the model; Phase 2 catches a URL served only in a non-probe region."""
    de_url = "https://static.tp-link.com/upload/gpl-code/2024/AX21_DE.tar.gz"
    p = tmp_path / "fw.json"
    p.write_text(json.dumps([{"key": "firmware/AX21v1_1.0.0_up_1700000000000.bin", "size": 100, "modified": "x"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "phppage/gpl-res-list" not in url:
            return httpx.Response(404)
        q = parse_qs(urlparse(url).query)
        if q.get("model", [""])[0].lower() != "archer ax21":
            return httpx.Response(200, text="")
        u = de_url if q.get("appPath", [""])[0] == "de" else "https://static.tp-link.com/upload/gpl-code/2024/AX21_US.tar.gz"
        return httpx.Response(200, text=f'<a href="{u}">x</a>')

    source = ModelSweepSource(firmware_json=p, gpl_urls=_none(tmp_path), regions=["us", "de"], probe_regions=["us"], concurrency=4)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert de_url in result
    assert "https://static.tp-link.com/upload/gpl-code/2024/AX21_US.tar.gz" in result
    assert {"us", "de"} <= source.verified_regions


def test_model_sweep_harvests_sibling_from_gpl_filename(tmp_path: Path) -> None:
    """A multi-model tarball returned in probe reveals a sibling model, probed in round 2."""
    bundle = "https://static.tp-link.com/upload/gpl-code/2024/EAP610v4_EAP613v2.tar.gz"
    sibling = "https://static.tp-link.com/upload/gpl-code/2024/EAP613_only.tar.gz"
    p = tmp_path / "fw.json"
    p.write_text(json.dumps([{"key": "firmware/EAP610v3_1.0_up_1700000000000.bin", "size": 100, "modified": "x"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "phppage/gpl-res-list" not in url:
            return httpx.Response(404)
        model = parse_qs(urlparse(url).query).get("model", [""])[0].lower()
        if model == "eap610":
            return httpx.Response(200, text=f'<a href="{bundle}">x</a>')  # reveals EAP613 in the filename
        if model == "eap613":
            return httpx.Response(200, text=f'<a href="{sibling}">x</a>')
        return httpx.Response(200, text="")

    source = ModelSweepSource(firmware_json=p, gpl_urls=_none(tmp_path), regions=["us"], probe_regions=["us"], concurrency=4)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    result = asyncio.run(_run())
    assert bundle in result
    assert sibling in result  # only reachable by harvesting EAP613 from the bundle filename


def test_model_sweep_collects_linked_models(tmp_path: Path) -> None:
    """?model= links on a region's gpl-code page are harvested and swept."""
    gpl = "https://static.tp-link.com/resources/gpl/Linked_GPL.tar.gz"
    p = tmp_path / "fw.json"
    p.write_text(json.dumps([{"key": "firmware/", "size": 0, "modified": "x"}]), encoding="utf-8")  # no firmware models

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "support/gpl-code" in url and "phppage" not in url:
            return httpx.Response(200, text='<script>var productTree={"1":[{"href":"?model=Archer ZZ99"}]};</script>')
        if "phppage/gpl-res-list" in url:
            model = parse_qs(urlparse(url).query).get("model", [""])[0].lower()
            return httpx.Response(200, text=f'<a href="{gpl}">x</a>') if model == "archer zz99" else httpx.Response(200, text="")
        return httpx.Response(404)

    source = ModelSweepSource(firmware_json=p, gpl_urls=_none(tmp_path), regions=["us"], probe_regions=["us"], concurrency=4)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> set[str]:
        async with client:
            return await source.run(client=client)

    assert gpl in asyncio.run(_run())  # model came only from the ?model= link, not firmware


def test_model_sweep_conforms_to_protocol() -> None:
    """ModelSweepSource structurally conforms to the ScrapeSource Protocol."""
    from tpwalk.scrape import ScrapeSource

    assert isinstance(ModelSweepSource(), ScrapeSource)
    assert ModelSweepSource().name == "model_sweep"
