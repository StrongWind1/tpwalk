# `tpwalk scrape`

Discover GPL archive URLs from every source and write one `.txt` file per source into a timestamped directory under `data/scrapes/`.

```bash
tpwalk scrape [OPTIONS]
```

## Options

| Option | Default | Description |
|---|---|---|
| `--data-dir` | `data` | Root data directory; the `scrapes/` subdirectory is written here. |
| `--model-sweep` | off | Also run the `phppage` model-wordlist sweep — heavy (thousands of requests to `www.tp-link.com`, several minutes). |
| `--sweep-max-models` | none | Cap the number of candidate models for `--model-sweep` (sorted; default no cap). |

## Sources

`scrape` runs each source concurrently under a shared async client; a failure in one source never aborts the others. Each source writes its own result file.

| Source | Output file | Needs `GITHUB_TOKEN` |
|---|---|:---:|
| Regional crawl (two-pass) | `regional_crawl.txt` | — |
| Wayback Machine CDX | `wayback_cdx.txt` | — |
| Common Crawl | `common_crawl.txt` | — |
| GitHub code search | `github_search.txt` | ✓ |
| TP-Link GitHub | `tplink_github.txt` | ✓ |
| Mercusys regional | `mercusys_regional.txt` | — |
| Reddit | `reddit.txt` | — |
| Forums | `forums.txt` | — |
| Google | `google.txt` | — |
| Model sweep *(opt-in)* | `model_sweep.txt` | — |

The GitHub sources are skipped with a notice unless `GITHUB_TOKEN` is set in the environment.

## Examples

```bash
# Run all passive sources
tpwalk scrape

# Include the GitHub sources
GITHUB_TOKEN=ghp_… tpwalk scrape

# Add the heavy model-wordlist sweep, capped at 500 models
tpwalk scrape --model-sweep --sweep-max-models 500

# Write to a custom data directory
tpwalk scrape --data-dir /tmp/tpwalk-run
```

When the run finishes, tpwalk prints a summary with per-source unique/raw counts. Feed the results to [`verify`](verify.md) next.
