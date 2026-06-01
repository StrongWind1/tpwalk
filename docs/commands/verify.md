# `tpwalk verify`

Read every discovered URL, deduplicate, HEAD-check each one against the S3 origin, and write the verified/dead manifests plus a runnable `s5cmd` download list.

```bash
tpwalk verify [OPTIONS]
```

## Options

| Option | Default | Description |
|---|---|---|
| `-c`, `--concurrency` | `100` | Maximum concurrent S3 HEAD requests. |
| `--data-dir` | `data` | Root data directory; reads `scrapes/` from here and writes output here. |

## What it reads

`verify` reads every `.txt` file under `{data-dir}/scrapes/` — both the curated `seed/` directory and every timestamped `scrape` run directory — then normalizes and deduplicates the URLs before checking them.

## Output files

All five files are written into `--data-dir` (default `data/`):

| File | Format | Contents |
|---|---|---|
| `verified.json` | JSON array | One object per live archive with full S3 metadata. |
| `verified.txt` | text | One `s3://` URL per line — direct input for `s5cmd`. |
| `s5cmd_download.txt` | text | A runnable `cp --if-size-differ` manifest for `s5cmd --no-sign-request run`. |
| `dead.json` | JSON array | One object per dead URL, classified as `http`, `timeout`, or `network`. |
| `dead.txt` | text | One `https://` URL per line. |

### `verified.json` schema

Each entry captures the S3 origin metadata, which is richer than what the CDN returns:

```json
{
  "url": "https://static.tp-link.com/upload/gpl-code/2024/202409/20240912/vigi_c485v1_gplcode.tar.gz",
  "size": 81923840,
  "etag": "\"…\"",
  "content_type": "application/gzip",
  "last_modified": "Thu, 12 Sep 2024 …",
  "version_id": "…",
  "replication_status": "COMPLETED",
  "encryption": "AES256",
  "server": "AmazonS3",
  "status": 200,
  "checked_at": "2026-06-01T00:00:00Z"
}
```

`version_id`, `replication_status`, and `encryption` are `null` for legacy files (pre-2022, under `/resources/gpl/`) and populated for modern files (2022+, under `/upload/gpl-code/`).

## Examples

```bash
# Verify everything discovered so far
tpwalk verify

# Raise concurrency for a faster pass
tpwalk verify -c 200

# Then download
s5cmd --no-sign-request run data/s5cmd_download.txt
```
