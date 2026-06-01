# tpwalk

**Discover, verify, and bulk-download every TP-Link GPL source release — from one command-line pipeline.**

Under the GPL, TP-Link must publish the corresponding source code for the open-source components in its firmware. That source lives in an S3-backed bucket (`static.tp-link.com`) that serves every file without authentication but blocks directory listing — so there is no index, and files surface only on scattered regional support pages. tpwalk rebuilds the index: it discovers GPL archive URLs from nine independent sources, HEAD-checks each one against the S3 origin for live status and metadata, and emits a ready-to-run `s5cmd` manifest for mirroring the whole corpus. The same technique covers the Mercusys sub-brand (`static.mercusys.com`).

This is a FOSS-compliance and archival tool. Every file it touches is source code a vendor is legally obligated to distribute and already serves publicly — see [Responsible use](guide/responsible-use.md).

## The pipeline

tpwalk is three subcommands that run in sequence:

1. **[`scrape`](commands/scrape.md)** — discover candidate GPL archive URLs from nine passive sources.
2. **[`verify`](commands/verify.md)** — deduplicate and HEAD-check every URL against the S3 origin, then write the manifests.
3. **`s5cmd run`** — mirror every confirmed-live archive using the generated download list.

```console
$ tpwalk scrape && tpwalk verify

Scrape complete
  Regions scraped:      37
  Unique (normalized):  1283
  …

Verify complete
  URLs found (unique):  1283
  Live:                 1187
  Dead:                 96

$ s5cmd --no-sign-request run data/s5cmd_download.txt   # download everything
```

When passive discovery is not enough, **[`bruteforce`](commands/bruteforce.md)** actively enumerates candidate URLs that no public page references.

## Next steps

- [Installation](getting-started/installation.md) — install tpwalk and `s5cmd`.
- [Quick start](getting-started/quick-start.md) — run the full pipeline in three commands.
- [How it works](guide/how-it-works.md) — the discovery sources, the S3 origin trick, and the output files.
