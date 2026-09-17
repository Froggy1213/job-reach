"""Fetching that needs a real browser, expressed as data.

Two of the four boards cannot be read with plain HTTP: Indeed Japan sits behind
Cloudflare, and Mynavi renders its results with JavaScript. Both need a browser
driven by a script — and both used to be the reason this plugin shipped its own
150 MB Playwright venv.

That is no longer necessary. `Scrapling <https://github.com/D4Vinci/Scrapling>`_
already provides stealth browsers (patched Chromium, Cloudflare solving) and is
very likely installed on this machine for its own MCP server. So fetching is
expressed as a **step list** — a small JSON vocabulary — and each backend
executes it:

* :func:`run_scrapling` ships the spec to ``scrapling_driver.py``, which runs
  inside the interpreter that has Scrapling installed;
* ``scrapers.base`` implements the same steps with Playwright, for machines
  that have the plugin's own venv but no Scrapling.

Keeping the steps as plain dicts (``wait``, ``scroll``, ``evaluate``, ``click``,
…) means a scraper describes *what to do to the page* without knowing which
browser does it, and that the two backends cannot drift: they are tested against
the same spec.

.. note::
   ``mode`` names the fetcher. ``stealthy`` is Scrapling's Cloudflare-capable
   browser; ``dynamic`` is its plain Playwright-driven browser; ``basic`` is
   plain HTTP (used for cheap robustness checks, not for scraping).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError, MissingDependencyError

#: Ceiling for one page fetch, in milliseconds. Cloudflare solving plus a JS
#: render can legitimately take 30 s; past that the site is not answering.
DEFAULT_TIMEOUT_MS = 90_000

#: How long to let the page settle after a scroll, before extracting.
DEFAULT_SETTLE_MS = 3_500

#: Chromium-on-macOS UA. Boards serve the same markup to everyone, but a
#: plausible identity avoids the crudest bot filters (the Scrapling browser
#: generates its own; this one is for the Playwright fallback).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)

#: Fetch modes, most capable first. A scraper may name a chain, so a board that
#: needs solving a challenge can still fall back to a plainer browser.
MODES = ("stealthy", "dynamic", "basic")

DEFAULT_LOCALE = "ja-JP"
DEFAULT_TIMEZONE = "Asia/Tokyo"

#: Environment override for the backend: ``auto`` (default), ``scrapling`` or
#: ``playwright``. Pinning it is how you debug one backend against the other.
BACKEND_ENV = "JOBREACH_BACKEND"


# --------------------------------------------------------------------------- #
# Step vocabulary
# --------------------------------------------------------------------------- #


def wait(ms: int) -> dict[str, Any]:
    """Pause for *ms* milliseconds (lets lazy content render)."""
    return {"wait_ms": int(ms)}


def scroll(px: int = 800, times: int = 1, *, settle_ms: int = 0) -> dict[str, Any]:
    """Scroll down *px* pixels *times*, pausing (if asked) after each step.

    Scrolling is how these boards load more cards: both Wantedly and Mynavi
    render the first screen server-side and hydrate the rest as you move.
    """
    return {"scroll": int(px), "times": max(1, int(times)), "wait_ms": int(settle_ms)}


def evaluate(js: str, key: str) -> dict[str, Any]:
    """Run *js* in the page and store its value under ``key``."""
    return {"evaluate": js, "key": key}


def click(selector: str, *, optional: bool = False, settle_ms: int = 0) -> dict[str, Any]:
    """Click *selector*.

    ``optional=True`` is the "click the pager if it exists" case: a missing
    element is not an error, because that is what the last page looks like.
    """
    return {"click": selector, "optional": bool(optional), "wait_ms": int(settle_ms)}


def wait_selector(selector: str, *, state: str = "attached", timeout_ms: int | None = None) -> dict[str, Any]:
    """Wait until *selector* reaches *state*."""
    step: dict[str, Any] = {"wait_selector": selector, "state": state}
    if timeout_ms is not None:
        step["timeout_ms"] = int(timeout_ms)
    return step


def wait_load(state: str = "domcontentloaded") -> dict[str, Any]:
    """Wait for a navigation lifecycle state (``load``, ``networkidle``, …)."""
    return {"wait_load": state}


def capture_html(key: str = "html") -> dict[str, Any]:
    """Store the page's current HTML under *key* (debugging aid)."""
    return {"capture": key}


def build_spec(
    url: str,
    steps: Sequence[dict[str, Any]] = (),
    *,
    mode: str = "stealthy",
    wait_selector: str | None = None,
    headless: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    locale: str = DEFAULT_LOCALE,
    timezone_id: str = DEFAULT_TIMEZONE,
    block_ads: bool = True,
) -> dict[str, Any]:
    """Assemble the JSON document both backends execute.

    Args:
        url: page to open.
        steps: the :func:`wait` / :func:`scroll` / :func:`evaluate` /
            :func:`click` list to run once the page is up.
        mode: which fetcher to use — see :data:`MODES`.
        wait_selector: CSS selector to wait for before running *steps*.
        headless: run the browser invisibly (the default; ``False`` when
            debugging a board's markup).
        timeout_ms: ceiling for the whole fetch.
        locale / timezone_id: the browser's locale and clock. Pinned to Japan
            so relative dates ("3日前") and prefecture names parse the way the
            card extractors expect.
        block_ads: drop requests to known ad/tracker domains (a Scrapling
            feature; ignored by the Playwright fallback).

    Raises:
        ConfigError: *mode* is not one of :data:`MODES`.
    """
    if mode not in MODES:
        raise ConfigError(f"unknown fetch mode {mode!r}; expected one of {', '.join(MODES)}")
    spec: dict[str, Any] = {
        "url": url,
        "mode": mode,
        "headless": bool(headless),
        "timeout_ms": int(timeout_ms),
        "locale": locale,
        "timezone_id": timezone_id,
        "block_ads": bool(block_ads),
        "steps": list(steps),
    }
    if wait_selector:
        spec["wait_selector"] = wait_selector
    return spec


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Backend:
    """The browser backend a scrape will use, and how it was chosen."""

    name: str
    detail: str
    #: Interpreter that runs the fetcher (Scrapling only; empty for Playwright,
    #: which runs inside this process).
    python: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "detail": self.detail, "python": self.python, "version": self.version}


def backend_preference() -> str:
    """Read ``$JOBREACH_BACKEND``, defaulting to ``auto``."""
    raw = os.environ.get(BACKEND_ENV, "").strip().lower()
    if raw in {"", "auto"}:
        return "auto"
    if raw in {"scrapling", "playwright"}:
        return raw
    raise ConfigError(
        f"{BACKEND_ENV}={raw!r} is not a backend; use 'auto', 'scrapling' or 'playwright'"
    )


def select_backend(preference: str | None = None) -> Backend:
    """Pick the browser backend for this process.

    ``auto`` prefers Scrapling: it solves Cloudflare challenges that plain
    Playwright cannot, and it is usually already installed (the MCP server
    ships it), which means no 150 MB Chromium download.

    Raises:
        MissingDependencyError: no usable backend, with a hint naming both fixes.
    """
    from .scrapling import (
        PLAYWRIGHT_FALLBACK_HINT,
        SCRAPLING_PYTHON_ENV,
        probe,
        scrapling_python,
    )

    wanted = (preference or backend_preference()).lower()

    if wanted in {"auto", "scrapling"}:
        python, source = scrapling_python()
        if python:
            outcome = probe(python)
            if outcome.ok:
                return Backend(
                    "scrapling",
                    f"{outcome.detail} [{source}]",
                    python=python,
                    version=outcome.version,
                )
            if wanted == "scrapling":
                raise MissingDependencyError(
                    f"Scrapling was requested but {python} cannot use it: {outcome.detail}",
                    hint=PLAYWRIGHT_FALLBACK_HINT,
                )
        elif wanted == "scrapling":
            raise MissingDependencyError(
                f"no interpreter with Scrapling found; set {SCRAPLING_PYTHON_ENV} to one",
                hint=PLAYWRIGHT_FALLBACK_HINT,
            )

    if wanted in {"auto", "playwright"}:
        from .scrapers.base import probe_playwright  # local import: avoids a cycle

        ok, detail = probe_playwright()
        if ok:
            return Backend("playwright", detail)
        if wanted == "playwright":
            raise MissingDependencyError(
                f"Playwright was requested but is not usable: {detail}",
                hint=PLAYWRIGHT_FALLBACK_HINT,
            )
        raise MissingDependencyError(
            "no browser backend is available: Scrapling is not installed and the "
            f"plugin's Playwright venv is missing ({detail})",
            hint=PLAYWRIGHT_FALLBACK_HINT,
        )

    raise ConfigError(f"unknown backend preference {wanted!r}")


@dataclass(slots=True)
class FetchResult:
    """What one page fetch produced, backend-independent."""

    status: int | None
    url: str
    title: str
    results: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    elapsed_s: float = 0.0

    def get(self, key: str, default: Any = None) -> Any:
        return self.results.get(key, default)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FetchResult:
        """Build from a driver/backend JSON payload."""
        return cls(
            status=payload.get("status"),
            url=str(payload.get("url") or ""),
            title=str(payload.get("title") or ""),
            results=payload.get("results") or {},
            blocked=bool(payload.get("blocked")),
            elapsed_s=float(payload.get("elapsed_s") or 0.0),
        )


def run_scrapling(spec: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """Execute *spec* with Scrapling, in the interpreter that provides it.

    Raises:
        MissingDependencyError: Scrapling is not available.
        ScraperError: the fetch itself failed.
    """
    from .scrapling import run_driver

    return run_driver(spec, timeout=timeout)
