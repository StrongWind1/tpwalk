# Installation

tpwalk requires **Python 3.14+** and is installed from source with [uv](https://docs.astral.sh/uv/).

## Install the CLI

```bash
git clone https://github.com/StrongWind1/tpwalk.git
cd tpwalk
uv tool install .
```

This installs the `tpwalk` command onto your `PATH`. Verify it:

```bash
tpwalk --help
```

## Install s5cmd (for downloading)

The `scrape` and `verify` commands have no external runtime requirements, but mirroring the discovered archives uses [`s5cmd`](https://github.com/peak/s5cmd). Install it from your package manager or its release page, then confirm:

```bash
s5cmd version
```

`verify` writes a ready-to-run manifest (`data/s5cmd_download.txt`) that you hand straight to `s5cmd --no-sign-request run` — see the [quick start](quick-start.md).

## Development setup

For working on tpwalk itself, sync the full environment instead:

```bash
uv sync          # create the dev environment (dev + docs groups)
make check       # ruff format + ruff check + ty + pytest
make docs-serve  # preview the documentation locally
```
