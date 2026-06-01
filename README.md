# tpwalk

**Discover, verify, and bulk-download every TP-Link GPL source release — from one command-line pipeline.**

[![CI](https://github.com/StrongWind1/tpwalk/actions/workflows/ci.yml/badge.svg)](https://github.com/StrongWind1/tpwalk/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue.svg)](https://strongwind1.github.io/tpwalk/)
[![Python](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CLI: Typer](https://img.shields.io/badge/CLI-Typer-blue.svg)](https://typer.tiangolo.com/)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Types: ty](https://img.shields.io/badge/types-ty-261230.svg)](https://github.com/astral-sh/ty)

Under the GPL, TP-Link must publish the corresponding source code for the open-source components in its firmware. That source lives in an S3-backed bucket (`static.tp-link.com`) that serves every file without authentication but blocks directory listing — so there is no index, and files surface only on scattered regional support pages. tpwalk rebuilds the index: it discovers GPL archive URLs from nine independent sources, HEAD-checks each one against the S3 origin for live status and metadata, and emits a ready-to-run `s5cmd` manifest for mirroring the whole corpus. The same technique covers the Mercusys sub-brand (`static.mercusys.com`).

This is a FOSS-compliance and archival tool. Every file it touches is source code a vendor is legally obligated to distribute and already serves publicly — see [Responsible use](#responsible-use).

**[Read the full documentation →](https://strongwind1.github.io/tpwalk/)**

## Features

- **Nine discovery sources** — regional GPL pages, Wayback Machine, Common Crawl, GitHub code search, TP-Link's own GitHub, Mercusys regional pages, Reddit, forums, and Google, each writing its own result file.
- **Two-pass regional crawl** — parses the `productTree` JSON embedded in every country's GPL page, then follows per-model `phppage` sub-pages that hide files the main page never links. Different regions expose different files; tpwalk scrapes all of them.
- **S3-origin verification** — HEAD-checks the raw S3 origin (not just the CDN) to capture `size`, `etag`, `last-modified`, version-id, replication status, and server-side encryption per file.
- **Runnable download manifest** — `verify` emits `s5cmd_download.txt`, a `cp --if-size-differ` script you hand straight to `s5cmd --no-sign-request run`.
- **Active brute-force enumeration** — optional date-path and model-name generators for files no public page references, with tiered coverage and hard safety valves.
- **Async + bounded concurrency** — HTTP/2 via `httpx`, a configurable concurrency ceiling, and a live Rich progress display with a live/dead counter.

## Example

```console
$ tpwalk scrape && tpwalk verify

Scrape complete
  Regions scraped:      37
  Regions failed:       0
  Pass 1 URLs:          756
  Pass 2 URLs:          420
  Raw total:            1834
  Unique (normalized):  1283
  Regional crawl URLs:  1176 unique (1834 raw)
  Wayback CDX URLs:     107 unique (612 raw)
  Common Crawl URLs:    44 unique (980 raw)
  GitHub search URLs:   skipped (no GITHUB_TOKEN)
  Mercusys URLs:        21 unique (21 raw)
  …

Verify complete
  URLs found (unique):  1283
  Live:                 1187
  Dead:                 96

$ s5cmd --no-sign-request run data/s5cmd_download.txt   # download everything
```

## Installation

tpwalk requires **Python 3.14+** and is installed from source with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/StrongWind1/tpwalk.git
cd tpwalk
uv tool install .          # installs the `tpwalk` command onto your PATH
```

Bulk downloading additionally requires [`s5cmd`](https://github.com/peak/s5cmd). For development, use `uv sync` instead (see [Development](#development)).

## Quick start

The pipeline is three steps — discover, verify, download:

```bash
# 1. Discover URLs from all passive sources → data/scrapes/<timestamp>/
tpwalk scrape

# 2. HEAD-check every discovered URL → data/verified.{json,txt}, data/dead.{json,txt}, data/s5cmd_download.txt
tpwalk verify

# 3. Mirror every confirmed-live archive with s5cmd
s5cmd --no-sign-request run data/s5cmd_download.txt
```

GitHub-based sources are skipped unless a token is present:

```bash
export GITHUB_TOKEN=ghp_…   # enables `github_search` and `tplink_github`
tpwalk scrape
```

## Commands

tpwalk exposes three subcommands. Run `tpwalk <command> --help` for the full option list.

### `tpwalk scrape`

Discovers GPL archive URLs from every source and writes one `.txt` file per source into a timestamped directory under `data/scrapes/`.

| Option | Default | Description |
|---|---|---|
| `--data-dir` | `data` | Root data directory; the `scrapes/` subdirectory is written here. |
| `--model-sweep` | off | Also run the `phppage` model-wordlist sweep — heavy (thousands of requests to `www.tp-link.com`, several minutes). |
| `--sweep-max-models` | none | Cap the number of candidate models for `--model-sweep`. |

### `tpwalk verify`

Reads every `.txt` under `data/scrapes/`, normalizes and deduplicates the URLs, HEAD-checks each against the S3 origin, and batch-writes the five output files.

| Option | Default | Description |
|---|---|---|
| `-c`, `--concurrency` | `100` | Maximum concurrent S3 HEAD requests. |
| `--data-dir` | `data` | Root data directory; reads `scrapes/` from here and writes output here. |

### `tpwalk bruteforce`

Actively enumerates candidate URLs the passive sources never reference, by crossing a date-path generator with a model-name generator and HEAD-checking each candidate.

| Option | Default | Description |
|---|---|---|
| `--strategy` | `all` | Which generator(s) to run: `dates`, `models`, or `all`. |
| `--thorough` | off | Model strategy over ~389 known GPL date directories (medium coverage, ~40M HEADs). |
| `--exhaustive` | off | Full model × date-path cross (~203M HEADs). Always pair with `--max-candidates`. |
| `--max-candidates` | none | Hard cap on candidates checked — the safety valve for the heavy tiers. |
| `--dry-run` | off | Count candidates without issuing any HEAD requests. |
| `-c`, `--concurrency` | `100` | Maximum concurrent S3 HEAD requests. |
| `--data-dir` | `data` | Root data directory; writes a timestamped run directory here. |

> The `--exhaustive` tier issues hundreds of millions of requests. Start with `--dry-run` to size the job, then bound it with `--max-candidates`.

## How it works

### Discovery sources

`scrape` runs each source concurrently under a shared async client; a failure in one source never aborts the others.

| Source | Output file | What it does | Needs token |
|---|---|---|:---:|
| Regional crawl | `regional_crawl.txt` | Two-pass crawl of per-country GPL pages: `productTree` JSON + `phppage` sub-pages | — |
| Wayback Machine | `wayback_cdx.txt` | Internet Archive CDX API across five URL prefixes, with `resumeKey` pagination | — |
| Common Crawl | `common_crawl.txt` | CC index API across all historical indices | — |
| GitHub search | `github_search.txt` | Code search for archive URLs across public repos | ✓ |
| TP-Link GitHub | `tplink_github.txt` | TP-Link's own GitHub organizations and repos | ✓ |
| Mercusys regional | `mercusys_regional.txt` | Per-region Mercusys GPL pages (`static.mercusys.com`) | — |
| Reddit | `reddit.txt` | Reddit search for archive links | — |
| Forums | `forums.txt` | Community forum threads | — |
| Google | `google.txt` | Google result parsing | — |
| Model sweep *(opt-in)* | `model_sweep.txt` | `phppage` model-wordlist sweep — enabled by `--model-sweep` | — |

### Why direct GETs work but listing doesn't

`static.tp-link.com` denies `ListBucket` on every prefix, so the bucket can't be enumerated — but a direct GET (or HEAD) of any known key succeeds with no authentication, on both the CloudFront CDN and the raw S3 origin. tpwalk verifies against the origin (`s3.amazonaws.com/static.tp-link.com`) because it returns richer metadata headers — version-id, replication status, and encryption — that the CDN strips. Two key conventions exist: a flat legacy layout (`/resources/gpl/<file>`, pre-2022) and a date-hierarchical modern layout (`/upload/gpl-code/YYYY/YYYYMM/YYYYMMDD/<file>`).

### Output files

`verify` writes five files into `--data-dir` (default `data/`):

| File | Format | Contents |
|---|---|---|
| `verified.json` | JSON array | One object per live archive with full S3 metadata (`size`, `etag`, `content_type`, `last_modified`, `version_id`, `replication_status`, `encryption`, `server`, `status`, `checked_at`). |
| `verified.txt` | text | One `s3://` URL per line — direct input for `s5cmd`. |
| `s5cmd_download.txt` | text | A runnable `cp --if-size-differ` manifest for `s5cmd --no-sign-request run`. |
| `dead.json` | JSON array | One object per dead URL, classified as `http`, `timeout`, or `network`. |
| `dead.txt` | text | One `https://` URL per line. |

## Repository structure

```
tpwalk/
├── cli.py              # Typer entry point — scrape / verify / bruteforce
├── _client.py          # shared httpx AsyncClient factory (HTTP/2, retries, headers)
├── _normalize.py       # URL canonicalization + s3:// / s5cmd line conversion
├── models.py           # frozen dataclasses: VerifiedEntry, DeadEntry, *Stats
├── scrape/             # nine discovery sources + the ScrapeRunner orchestrator
├── verify/             # read → dedupe → HEAD-check → write the five output files
└── bruteforce/         # active date-path / model-name enumeration
tests/                  # pytest suite (mocked HTTP — no live network)
data/                   # discovered URLs (scrapes/, incl. curated seed/) and verify output
```

## Development

tpwalk uses [uv](https://docs.astral.sh/uv/) for environment management, [Ruff](https://github.com/astral-sh/ruff) for linting and formatting, [ty](https://github.com/astral-sh/ty) for type-checking, and pytest for tests.

```bash
uv sync              # create the dev environment
make check           # ruff format --check + ruff check + ty + pytest
make format          # auto-fix formatting and lint
make test            # pytest only
```

The test suite mocks all HTTP — it never touches the live network, so it is safe to run anywhere.

## Responsible use

The GPL and LGPL require any party distributing covered binaries to make the corresponding source code available. tpwalk only locates and mirrors that license-mandated source, which TP-Link and Mercusys already publish on public, unauthenticated CDNs. It performs read-only requests (HEAD and GET) and never attempts to access non-public data.

That said, be a good neighbor: keep `--concurrency` reasonable, prefer the passive `scrape`/`verify` path over brute force, and treat the `--thorough`/`--exhaustive` tiers as deliberate, bounded jobs rather than defaults. You are responsible for complying with the robots directives and terms of service of every source the scraper touches.

## License

[MIT](LICENSE)
