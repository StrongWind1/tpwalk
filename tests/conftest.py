"""Shared pytest fixtures for the tpwalk test suite.

Provides a temporary scrapes directory and a representative sample of URLs
covering all encoding chaos classes present in the real seed data.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def scrapes_dir(tmp_path: Path) -> Path:
    """Return a temporary scrapes directory with seed/ already created."""
    seed = tmp_path / "scrapes" / "seed"
    seed.mkdir(parents=True)
    return tmp_path / "scrapes"


@pytest.fixture
def sample_urls() -> list[str]:
    """Return representative URLs covering the four encoding chaos classes.

    Covers:
    - Legacy flat path (no spaces, no special chars)
    - Raw space in path (EAP Controller)
    - Literal parentheses in filename (A10(JP)V1)
    - Mercusys sub-brand domain
    """
    return [
        "https://static.tp-link.com/resources/gpl/11N_GPL.tgz",
        "https://static.tp-link.com/2018/201804/20180404/EAP Controller_V2.5_GPL.zip",
        "https://static.tp-link.com/resources/gpl/A10(JP)V1_GPL.tar.bz2",
        "https://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz",
    ]


@pytest.fixture(autouse=True)
def _instant_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op ``asyncio.sleep`` across the suite so retry/backoff paths run instantly.

    Every scrape source backs off on transient HTTP failures with
    ``await asyncio.sleep(2.0 * (2**attempt))`` (2s, 4s, 8s per D-07) plus
    Retry-After sleeps. Tests drive those failure paths with mocked transports,
    so the delay is pure dead wall-clock: a single retry-exhaustion test sleeps
    14s for real, and several such tests across sources push the full suite past
    pytest's practical timeout (observed as a "stuck" run). Patching the real
    ``asyncio.sleep`` makes backoff instant in tests; production behavior is
    untouched. Individual tests that assert on backoff timing override this with
    their own mock, which still works because the test-body monkeypatch wins.
    """

    async def _instant(_delay: float, *_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.fixture(autouse=True)
def _plain_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force Typer/Rich --help output to plain text so flag assertions are stable.

    Typer renders --help through Rich. When the environment forces color (CI sets
    ``FORCE_COLOR``), Rich emits ANSI escape codes that split option names like
    ``--data-dir`` across escape sequences, so ``"--data-dir" in result.output``
    fails even though the flag is present. ``NO_COLOR`` does not override
    ``FORCE_COLOR``, but a dumb terminal does — we clear the color-forcing vars and
    set ``TERM=dumb`` so the rendered help matches what a user sees through a plain
    pipe, making the CLI help tests deterministic across local runs and CI.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLORS", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
