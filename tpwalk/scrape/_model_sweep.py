"""Model-wordlist phppage sweep — recovers GPL for models not linked in any region tree.

The regional crawler (Pass 2, see ``_regional.py``) only queries the phppage
endpoint for models that appear as ``?model=`` items in some region's productTree.
But phppage accepts ANY model name -- the match is case-insensitive yet otherwise
exact on the canonical base name (``EAP610`` works, ``EAP610v3`` does not; ``Archer
C7`` works, ``ArcherC7`` does not) -- and returns that model's full GPL revision
list. This source builds the most complete model wordlist it can and sweeps phppage
across every live region, surfacing GPL archives the tree-driven crawl never asks for.

Model-name sources (unioned -- phppage is the oracle, so over-generating only costs
a wasted probe request):

* Firmware S3 listing (``ref_gpl_data/firmware_s3_listing.json``): ~52k firmware
  keys normalized to base model tokens (+ brand-prefix variants, since firmware
  drops the brand: ``AX21`` -> ``Archer AX21``).
* Known GPL archive filenames (``ref_gpl_data/gpl_urls_master.txt``): closer to
  canonical names, and multi-model tarballs reveal siblings (``EAP610v4_EAP613v2``
  -> EAP610, EAP613).
* Live ``?model=`` harvest: every region's gpl-code page links models via phppage
  ``?model=`` hrefs; their union across all regions is the authoritative linked set.
* Live sibling harvest: GPL filenames returned mid-sweep are re-parsed for new
  model tokens, which are probed in a second round.

Two phases keep request volume sane against thousands of candidates:

* Phase 1 (probe): query each candidate in a few high-coverage probe regions and
  keep only those returning at least one GPL URL. Non-canonical forms return nothing.
* Phase 2 (expand): for validated models, query the remaining regions. ``appPath``
  filters results by region (verified live -- e.g. Deco BE95: us=2, de=1, jp=0), so
  expansion catches region-specific GPL variants the probe regions miss.

Regions swept are the live TP-Link regions (``discover_regions``); ``verified_regions``
records which ones actually answered. Heavy and opt-in: wired into ``scrape`` behind
``--model-sweep``. Per the phppage analysis in reference_tplink_phppage.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from tpwalk.scrape._regional import _PHPPAGE_URL, GPL_PAGE_TEMPLATE, _extract_phppage_urls, discover_regions

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rich.progress import Progress, TaskID

_log = logging.getLogger(__name__)

# --- Configuration ---

_DEFAULT_FIRMWARE_JSON = Path("ref_gpl_data/firmware_s3_listing.json")
_DEFAULT_GPL_URLS = Path("ref_gpl_data/gpl_urls_master.txt")

# Probe regions: the largest, most diverse productTrees (verified item counts:
# us=393, de=261, fr=246, in=76, br=73, jp=39). A model's "home" region is almost
# always among these, so a candidate that returns nothing here is dropped cheaply.
_DEFAULT_PROBE_REGIONS: tuple[str, ...] = ("us", "de", "fr", "in", "br", "jp")

# Politeness jitter before each request -- mirrors the CDX sources. No-op'd in tests.
_JITTER_MIN_S: float = 0.05
_JITTER_MAX_S: float = 0.25

# Model-token length bounds: shorter is noise, longer is a mangled filename.
_MIN_MODEL_LEN: int = 2
_MAX_MODEL_LEN: int = 40

# --- Model-name extraction ---

_EXT = re.compile(r"\.(bin|zip|gz|rollback|scv|ico|exe|deb|rar|img|tar|bz2|crdownload|tgz|xz|7z)$", re.IGNORECASE)
_ARCHIVE_EXT = re.compile(r"(?i)\.(tar\.gz|tar\.bz2|tar\.xz|tgz|tar|zip|rar|gz|bz2|xz|7z)$")
_GPL_MARKERS = re.compile(r"(?i)[._-]*(?:gpl|code|source|src|new)[._-]*")
_BRANDS = "Tapo|Archer|Deco|Vigi|Festa|Aginet|Kasa|Omada|Neffos|Mercusys"
_BRAND_SPLIT = re.compile(rf"^({_BRANDS})(?=[A-Za-z0-9])", re.IGNORECASE)
_MODEL_RE = re.compile(r"\?model=([^\"'&]+)")  # productTree phppage links
_REGION_TAGS: frozenset[str] = frozenset(
    {"US", "EU", "UN", "JP", "CN", "RU", "BR", "AU", "KR", "TH", "IN", "EG", "KZ", "UK", "CA", "MX", "TR", "VN", "ID", "MY", "SG", "PH", "AE", "SA", "IL", "UA", "KG", "UZ", "RO", "PL", "CZ", "HU", "GR", "PT", "ES", "IT", "FR", "DE", "NL", "BE", "CH", "AT", "DK", "FI", "NO", "SE", "HK", "TW", "EN", "ROW", "WW", "LA"}
)
_REGION_ALT = "|".join(sorted(_REGION_TAGS, key=len, reverse=True))
# First marker that terminates the model name: a glued version (``v5`` after a
# digit), a separated version (``_V14`` / `` v3``), a dotted firmware version, a
# 6+ digit timestamp/build, a region/lang tag, an ISO date, or build/up/rel noise.
_VERSION = re.compile(rf"(?i)(?:(?<=\d)v\d|[ _-]v\d|[ _-]V\d|[ _-]\d+\.\d|[ _-]?\d{{6,}}|[ _-](?:{_REGION_ALT})\b|\bBuild\b|[ _-]up[ _-]|[ _-]rel|[ _-]signed|\d{{4}}-\d\d-\d\d)")
_VERSION_TOKEN = re.compile(r"(?i)v?\d+(\.\d+)*[a-z]?")

# Series prefixes firmware/GPL filenames usually strip but phppage needs the brand
# for (firmware "AX21" -> canonical "Archer AX21"; "X60" -> "Deco X60").
_ARCHER_SERIES = re.compile(r"(?i)^(?:AX|AXE|AC|GX|BE|EX|A|C|GE)\d")
_DECO_SERIES = re.compile(r"(?i)^(?:X|XE|BE|PX|M|S|P)\d")


def _normalize_model(name: str) -> str:
    """Reduce a firmware/GPL filename fragment to its canonical-ish base model token.

    Best-effort: strips extension and a leading ``(B)`` hardware-rev marker, drops
    bracketed region tags, cuts at the first version/region/date marker, and drops
    trailing version/region tokens. ``_BRAND_SPLIT`` re-inserts the space in glued
    brand forms (``TapoC200`` -> ``Tapo C200``). phppage validates the result, so
    residual noise costs only a wasted request.
    """
    b = _EXT.sub("", name)
    b = re.sub(r"^\([A-Z]\)\s*", "", b)  # (B)/(C) hardware-rev prefix
    b = b.replace("_", " ")
    b = re.sub(r"[(\[][^)\]]*[)\]]", " ", b)  # drop bracketed tags like (US), [rel...]
    b = _BRAND_SPLIT.sub(r"\1 ", b)
    m = _VERSION.search(b)
    if m:
        b = b[: m.start()]
    toks = b.strip(" _-").split()
    while toks and (toks[-1].upper() in _REGION_TAGS or _VERSION_TOKEN.fullmatch(toks[-1])):
        toks.pop()
    return re.sub(r"\s+", " ", " ".join(toks)).strip()


def _model_variants(token: str) -> set[str]:
    """Expand one normalized token into candidate phppage model names (brand guesses)."""
    out = {token}
    has_brand = bool(_BRAND_SPLIT.match(token))
    if not has_brand and _ARCHER_SERIES.match(token):
        out.add(f"Archer {token}")
    if not has_brand and _DECO_SERIES.match(token):
        out.add(f"Deco {token}")
    return out


def _keep(token: str) -> bool:
    """Return True when a token is a sane length, has a letter, and is not numeric-led."""
    return _MIN_MODEL_LEN <= len(token) <= _MAX_MODEL_LEN and bool(re.search(r"[A-Za-z]", token)) and not token[0].isdigit()


def _models_from_firmware(firmware_json: Path) -> set[str]:
    """Candidate models from the offline firmware S3 listing (broad, includes unlinked)."""
    data = json.loads(firmware_json.read_text(encoding="utf-8"))
    models: set[str] = set()
    for entry in data:
        key = entry.get("key", "")
        if entry.get("size", 0) <= 0 or not key.startswith("firmware/"):
            continue
        token = _normalize_model(key.rsplit("/", 1)[-1])
        if _keep(token):
            models |= _model_variants(token)
    return models


def _models_from_gpl_url(url: str) -> set[str]:
    """Candidate models parsed from one GPL archive URL's filename (splits multi-model tarballs)."""
    fn = _ARCHIVE_EXT.sub("", url.rsplit("/", 1)[-1])
    fn = _GPL_MARKERS.sub("_", fn)
    out: set[str] = set()
    for chunk in re.split(r"[_]+", fn):  # multi-model tarballs join models with _
        token = _normalize_model(chunk)
        if _keep(token):
            out |= _model_variants(token)
    return out


def load_model_wordlist(firmware_json: Path = _DEFAULT_FIRMWARE_JSON, gpl_urls: Path = _DEFAULT_GPL_URLS) -> set[str]:
    """Build the offline candidate model wordlist: firmware names plus GPL-filename names."""
    models = _models_from_firmware(firmware_json)
    if gpl_urls.exists():
        for line in gpl_urls.read_text(encoding="utf-8").splitlines():
            if line.strip():
                models |= _models_from_gpl_url(line.strip())
    return models


@dataclass(frozen=True)
class _SweepCtx:
    """Shared per-run request context (mirrors _github._GitHubCtx)."""

    client: httpx.AsyncClient
    sem: asyncio.Semaphore


def _set_total(progress: Progress | None, task: TaskID | None, total: int) -> None:
    """Set a progress bar's total and reveal it, when progress is wired up."""
    if progress is not None and task is not None:
        progress.update(task, total=total, visible=True)


def _advance(progress: Progress | None, task: TaskID | None) -> None:
    """Advance a progress bar by one, when progress is wired up."""
    if progress is not None and task is not None:
        progress.update(task, advance=1)


class ModelSweepSource:
    """phppage model-wordlist sweep (opt-in, heavy). Conforms to the ScrapeSource Protocol."""

    @property
    def name(self) -> str:
        """Filename stem: ScrapeRunner writes 'model_sweep.txt'."""
        return "model_sweep"

    def __init__(  # noqa: PLR0913 -- configuration constructor: data-source paths + sweep knobs
        self,
        *,
        firmware_json: Path = _DEFAULT_FIRMWARE_JSON,
        gpl_urls: Path = _DEFAULT_GPL_URLS,
        regions: Iterable[str] | None = None,
        probe_regions: Iterable[str] = _DEFAULT_PROBE_REGIONS,
        concurrency: int = 16,
        max_models: int | None = None,
    ) -> None:
        """Configure the sweep.

        Args:
            firmware_json: Firmware S3 listing path (model-name source).
            gpl_urls: Known GPL URL list path (model-name source).
            regions: Explicit valid appPath regions; when None, discovered live.
            probe_regions: Phase-1 probe set (high-coverage regions).
            concurrency: Max concurrent phppage requests.
            max_models: Optional cap on candidate models (sorted), for bounded runs.

        """
        self._firmware_json = firmware_json
        self._gpl_urls = gpl_urls
        self._regions = frozenset(regions) if regions is not None else None
        self._probe_regions = tuple(probe_regions)
        self._concurrency = concurrency
        self._max_models = max_models
        self.validated_models: int = 0  # populated by run()
        self.verified_regions: set[str] = set()  # regions that actually answered

    async def run(
        self,
        *,
        client: httpx.AsyncClient,
        progress: Progress | None = None,
        task_pass1: TaskID | None = None,
        task_pass2: TaskID | None = None,
    ) -> set[str]:
        """Sweep phppage across the model wordlist x all live regions; return GPL URLs."""
        ctx = _SweepCtx(client=client, sem=asyncio.Semaphore(self._concurrency))
        regions = sorted(self._regions) if self._regions is not None else sorted(await discover_regions(client=client, sem=asyncio.Semaphore(50)))
        probe = [r for r in self._probe_regions if r in set(regions)] or regions[:4]

        seed = load_model_wordlist(self._firmware_json, self._gpl_urls)
        link = await self._collect_link_models(ctx=ctx, regions=regions)  # ?model= from every region
        models = sorted(seed | link)
        if self._max_models is not None:
            models = models[: self._max_models]
        _log.info("ModelSweep: %d candidates (%d firmware/gpl + %d linked) x %d regions (probe=%d)", len(models), len(seed), len(link), len(regions), len(probe))

        # Phase 1: probe, then one harvest round on GPL siblings found in the responses.
        validated = await self._probe_phase(ctx=ctx, models=models, probe=probe, progress=progress, task=task_pass1)
        harvested = sorted(self._harvest_siblings(validated) - set(models))
        if harvested:
            _log.info("ModelSweep: harvested %d new model tokens from GPL filenames; probing", len(harvested))
            validated.update(await self._probe_phase(ctx=ctx, models=harvested, probe=probe, progress=None, task=None))

        self.validated_models = len(validated)
        all_urls: set[str] = {u for urls in validated.values() for u in urls}
        _log.info("ModelSweep: %d candidates validated (%d URLs after probe)", len(validated), len(all_urls))

        # Phase 2: expand validated models across the remaining regions.
        expand_regions = [r for r in regions if r not in set(probe)]
        all_urls |= await self._expand_phase(ctx=ctx, models=list(validated), regions=expand_regions, progress=progress, task=task_pass2)
        _log.info("ModelSweep total: %d URLs from %d models; %d/%d regions verified", len(all_urls), len(validated), len(self.verified_regions), len(regions))
        return all_urls

    async def _collect_link_models(self, *, ctx: _SweepCtx, regions: list[str]) -> set[str]:
        """Harvest every region's ``?model=`` phppage links into one canonical model set."""
        models: set[str] = set()

        async def _one(region: str) -> None:
            async with ctx.sem:
                await asyncio.sleep(random.uniform(_JITTER_MIN_S, _JITTER_MAX_S))  # noqa: S311 -- jitter, not cryptographic
                try:
                    r = await ctx.client.get(GPL_PAGE_TEMPLATE.format(region=region), follow_redirects=True)
                except httpx.TimeoutException, httpx.RequestError:
                    return
            if r.status_code == httpx.codes.OK:
                models.update(m.strip() for m in _MODEL_RE.findall(r.text) if _keep(m.strip()))

        async with asyncio.TaskGroup() as tg:
            for region in regions:
                tg.create_task(_one(region))
        return models

    @staticmethod
    def _harvest_siblings(validated: dict[str, set[str]]) -> set[str]:
        """Re-parse GPL filenames returned during probe for new (e.g. multi-model) tokens."""
        out: set[str] = set()
        for urls in validated.values():
            for url in urls:
                out |= _models_from_gpl_url(url)
        return out

    async def _probe_phase(self, *, ctx: _SweepCtx, models: list[str], probe: list[str], progress: Progress | None, task: TaskID | None) -> dict[str, set[str]]:
        """Phase 1: probe each candidate in the probe regions; keep those with GPL."""
        validated: dict[str, set[str]] = {}
        _set_total(progress, task, len(models))

        async def _probe(model: str) -> None:
            urls: set[str] = set()
            for region in probe:
                urls |= await self._fetch(ctx=ctx, model=model, region=region)
            if urls:
                validated[model] = urls
            _advance(progress, task)

        async with asyncio.TaskGroup() as tg:
            for model in models:
                tg.create_task(_probe(model))
        return validated

    async def _expand_phase(self, *, ctx: _SweepCtx, models: list[str], regions: list[str], progress: Progress | None, task: TaskID | None) -> set[str]:
        """Phase 2: query each validated model across the remaining regions."""
        found: set[str] = set()
        _set_total(progress, task, len(models))

        async def _expand(model: str) -> None:
            for region in regions:
                found.update(await self._fetch(ctx=ctx, model=model, region=region))
            _advance(progress, task)

        async with asyncio.TaskGroup() as tg:
            for model in models:
                tg.create_task(_expand(model))
        return found

    async def _fetch(self, *, ctx: _SweepCtx, model: str, region: str) -> set[str]:
        """Fetch one phppage fragment and extract GPL URLs; record verified regions. Never raises."""
        async with ctx.sem:
            await asyncio.sleep(random.uniform(_JITTER_MIN_S, _JITTER_MAX_S))  # noqa: S311 -- jitter, not cryptographic
            try:
                r = await ctx.client.get(
                    _PHPPAGE_URL,
                    params={"model": model, "appPath": region},
                    headers={"Referer": f"https://www.tp-link.com/{region}/support/gpl-code/"},
                )
            except httpx.TimeoutException, httpx.RequestError:
                return set()
        if r.status_code != httpx.codes.OK:
            return set()
        urls = set(_extract_phppage_urls(r.text))
        if urls:
            self.verified_regions.add(region)
        return urls
