# Responsible use

tpwalk is a FOSS-compliance and archival tool, not an exploitation tool.

The GPL and LGPL require any party distributing covered binaries to make the corresponding source code available. tpwalk only locates and mirrors that license-mandated source, which TP-Link and Mercusys already publish on public, unauthenticated CDNs. It performs read-only requests (`HEAD` and `GET`) and never attempts to access non-public data.

That said, be a good neighbour:

- **Keep concurrency reasonable.** The default `--concurrency 100` is plenty for the S3 origin; there is rarely a reason to go higher.
- **Prefer passive discovery.** The [`scrape`](../commands/scrape.md) → [`verify`](../commands/verify.md) path finds the corpus with a modest request budget. Reach for [`bruteforce`](../commands/bruteforce.md) only when you have evidence of files the public pages don't list.
- **Treat the heavy tiers as deliberate jobs.** `--thorough` and `--exhaustive` issue tens to hundreds of millions of requests. Size them with `--dry-run` and bound them with `--max-candidates` — never run them unbounded.
- **Respect upstream.** You are responsible for complying with the robots directives and terms of service of every source the scraper touches.
