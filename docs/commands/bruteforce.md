# `tpwalk bruteforce`

Actively enumerate candidate URLs that the passive sources never reference, by crossing a date-path generator with a model-name generator and HEAD-checking each candidate against the S3 origin.

```bash
tpwalk bruteforce [OPTIONS]
```

!!! warning "This issues a lot of requests"
    The heavy tiers generate tens to hundreds of millions of candidate URLs. Always start with `--dry-run` to size the job, and bound it with `--max-candidates`.

## Options

| Option | Default | Description |
|---|---|---|
| `--strategy` | `all` | Which generator(s) to run: `dates`, `models`, or `all`. |
| `--thorough` | off | Model strategy over ~389 known GPL date directories (medium coverage, ~40M HEADs). |
| `--exhaustive` | off | Full model × date-path cross (~203M HEADs). Always pair with `--max-candidates`. |
| `--max-candidates` | none | Hard cap on candidates checked — the safety valve for the heavy tiers. |
| `--dry-run` | off | Count candidates without issuing any HEAD requests. |
| `-c`, `--concurrency` | `100` | Maximum concurrent S3 HEAD requests. |
| `--data-dir` | `data` | Root data directory; writes a timestamped run directory here. |

## Strategies

- **`dates`** — walks the date-hierarchical `/upload/gpl-code/YYYY/YYYYMM/YYYYMMDD/` prefix space.
- **`models`** — crosses extracted firmware model names with the empirical GPL filename patterns mined from the known corpus.
- **`all`** — runs both.

!!! note "Model strategy needs the firmware listing"
    The model strategy extracts model tokens from `firmware_s3_listing.json` (an 8.7 MB index that is **not** bundled with the distribution). Without it the model strategy produces zero candidates. The small GPL filename-pattern corpus *is* bundled, so the date strategy and the pattern set work out of the box.

## Examples

```bash
# Size the full job without touching the network
tpwalk bruteforce --exhaustive --dry-run

# Date paths only, capped
tpwalk bruteforce --strategy dates --max-candidates 1000000

# Thorough model sweep, bounded
tpwalk bruteforce --strategy models --thorough --max-candidates 500000
```

Confirmed-live hits are appended to per-strategy `.txt` files in a timestamped run directory; run [`verify`](verify.md) afterward to fold them into the manifests.
