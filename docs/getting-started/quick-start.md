# Quick start

The pipeline is three steps — discover, verify, download.

## 1. Discover

```bash
tpwalk scrape
```

This runs every passive discovery source and writes one `.txt` file per source into a timestamped directory under `data/scrapes/`. To also enable the GitHub-based sources, export a token first:

```bash
export GITHUB_TOKEN=ghp_…   # enables github_search and tplink_github
tpwalk scrape
```

See [`scrape`](../commands/scrape.md) for all options.

## 2. Verify

```bash
tpwalk verify
```

`verify` reads every `.txt` under `data/scrapes/`, normalizes and deduplicates the URLs, HEAD-checks each one against the S3 origin, and writes five files into `data/`:

| File | Contents |
|---|---|
| `verified.json` | Live archives with full S3 metadata |
| `verified.txt` | One `s3://` URL per line |
| `s5cmd_download.txt` | Runnable `s5cmd` manifest |
| `dead.json` / `dead.txt` | URLs that did not resolve |

See [`verify`](../commands/verify.md) for details and the full output schema.

## 3. Download

```bash
s5cmd --no-sign-request run data/s5cmd_download.txt
```

This mirrors every confirmed-live archive. The manifest uses `cp --if-size-differ`, so re-running it only fetches new or changed files.

## Going further

If passive discovery misses files you expect to exist, run an active enumeration pass with [`bruteforce`](../commands/bruteforce.md) — start with `--dry-run` to size the job.
