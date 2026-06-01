# How it works

## The bucket

TP-Link serves its GPL source archives from an S3-backed CDN at `static.tp-link.com`. The bucket denies `ListBucket` on every prefix, so it cannot be enumerated — but a direct `GET` (or `HEAD`) of any known key succeeds with no authentication, on both the CloudFront CDN and the raw S3 origin.

tpwalk verifies against the **origin** (`s3.amazonaws.com/static.tp-link.com`) rather than the CDN, because the origin returns richer metadata headers — object version id, cross-region replication status, and server-side encryption — that CloudFront strips.

## URL conventions

Two key layouts exist, split roughly at 2022:

- **Legacy (pre-2022):** `/resources/gpl/<file>` — a flat directory with inconsistent filenames.
- **Modern (2022+):** `/upload/gpl-code/YYYY/YYYYMM/YYYYMMDD/<file>` — date-hierarchical, trending toward `GPL_<model>.tar.gz`.

## Two-pass regional crawl

The richest passive source is TP-Link's own per-country support pages at `https://www.tp-link.com/<region>/support/gpl-code/`. Crucially, **different regions link different files** — scraping one country misses hundreds of archives.

- **Pass 1** parses the `productTree` JSON embedded in each region's page for direct download links.
- **Pass 2** follows the per-model `phppage` sub-pages that some products use instead of a direct link, extracting the archive URL each one returns.

## Discovery sources

[`scrape`](../commands/scrape.md) unions nine independent sources (plus an opt-in model sweep). Because each source finds a different slice of the corpus, the union recovers far more than any single source — and the Wayback Machine and Common Crawl surface files that have since been delisted from the live pages.

## From discovery to download

[`verify`](../commands/verify.md) is where raw discovery becomes a clean, downloadable index: it deduplicates the URLs, confirms which are still live, records their S3 metadata, and emits `s5cmd_download.txt` — a runnable manifest you hand to `s5cmd --no-sign-request run`. The `cp --if-size-differ` form means re-running the download is incremental.

## When passive isn't enough

[`bruteforce`](../commands/bruteforce.md) fills the gaps the public pages never expose, by constructing candidate URLs from a date-path generator and a model-name generator and HEAD-checking them directly. It is gated behind explicit coverage tiers and a `--max-candidates` safety valve because the full cross is enormous.
