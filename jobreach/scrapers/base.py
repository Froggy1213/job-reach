"""Scraper strategy interface.

Every job board is one :class:`BaseScraper` subclass, and the orchestrator in
:mod:`jobreach.pipeline` treats them polymorphically — it calls ``fetch_jobs()``
and never knows which site it is talking to.

Three implementation paths:

1. **JSON APIs** (Wantedly) — subclass and call
   :mod:`jobreach.webclient`; no browser at all, and
   :attr:`BaseScraper.needs_browser` is ``False`` so the plugin never demands a
   browser stack for a board that does not use one.
2. **JS-rendered or protected boards** (Indeed Japan, Mynavi) — build a *step
   list* and hand it to :meth:`BaseScraper.fetch`, which runs it on whichever
   browser backend is available (Scrapling, or Playwright as a fallback).
3. **CLI-driven boards** (LinkedIn via ``opencli``) — subclass
   :class:`~jobreach.scrapers.cli_base.CliScraper` instead.

Playwright is imported **lazily, inside the methods that need it**. That is what
lets the rest of the plugin run on a bare Python interpreter: importing
``jobreach`` must never require a browser stack.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from ..domain import JobPosting, SourcePlatform
from ..errors import MissingDependencyError, ScraperError
from ..fetchers import (
    DEFAULT_TIMEOUT_MS,
    USER_AGENT,
    FetchResult,
    build_spec,
    evaluate,
    run_scrapling,
    scroll,
    select_backend,
    wait,
)
from ..logging_setup import get_logger

logger = get_logger("scrapers")

#: Retries for transient navigation failures (the site, not the parser).
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0

PLAYWRIGHT_HINT = (
    'Install the scraping extra into the plugin venv:\n'
    '  hermes job-reach setup\n'
    'or manually:\n'
    '  uv pip install -e ".[scrape]" && uv run playwright install chromium'
)


def require_playwright() -> tuple[Any, Any]:
    """Import Playwright and playwright-stealth, or fail with a usable hint.

    Returns:
        ``(async_playwright, Stealth)``.

    Raises:
        MissingDependencyError: when the scraping extra is not installed.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingDependencyError(
            "the 'playwright' package is not installed in the interpreter "
            "running this scrape",
            hint=PLAYWRIGHT_HINT,
        ) from exc
    try:
        from playwright_stealth import Stealth
    except ImportError:  # optional hardening — a plain browser still works
        Stealth = None  # type: ignore[assignment]
    return async_playwright, Stealth


def probe_playwright() -> tuple[bool, str]:
    """Whether this interpreter can drive Playwright, and a one-line reason.

    The mirror of :func:`jobreach.scrapling.probe`, so backend selection can
    compare the two without either one raising.
    """
    try:
        require_playwright()
    except MissingDependencyError as exc:
        return False, str(exc)
    return True, "Playwright available in the engine interpreter"


def probe_browser_stack() -> tuple[str, str]:
    """Return ``(backend, detail)`` for the browser backend a scrape would use.

    This is the single interception point for "can this machine scrape at all?",
    called by the CLI's preflight and by ``doctor``.

    Raises:
        MissingDependencyError: no backend is usable, with a hint naming both
            fixes (install Scrapling, or run ``job setup`` for Playwright).
    """
    backend = select_backend()
    return backend.name, backend.detail


class BaseScraper(ABC):
    """Abstract strategy for one job board.

    Subclasses must implement :attr:`platform` and :meth:`fetch_jobs`.
    """

    #: Chrome-on-macOS UA. Boards serve the same markup to everyone, but a
    #: plausible UA avoids the crudest bot filters. (Scrapling generates its
    #: own; this is the Playwright fallback's identity.)
    user_agent: str = USER_AGENT

    #: ``True`` when the board filters by keyword server-side (its search URL
    #: carries the query), so titles returned are already relevant and the
    #: client-side keyword filter must be skipped. Critical for cross-language
    #: queries: an English "engineer" search returns Japanese エンジニア titles
    #: that a literal substring match would wrongly reject.
    url_encodes_keyword: bool = False

    #: ``False`` for boards read over plain HTTP — the CLI then never asks for
    #: a browser stack on their behalf.
    needs_browser: bool = True

    #: One line describing how a browser-free board is read; shown by
    #: ``doctor`` so "no browser" never has to be taken on faith.
    http_note: str = "read over HTTP — no browser involved"

    #: Browser fetch modes to try, in order. Scrapling's stealth browser can
    #: solve a Cloudflare challenge that a plain browser cannot, so a board may
    #: name a chain and let the first working mode win.
    fetch_modes: tuple[str, ...] = ("stealthy", "dynamic")

    #: Words that disqualify a title no matter what (used only for the default,
    #: keyword-less design search).
    stop_words: frozenset[str] = frozenset(
        {
            "cad", "mechanical", "machine", "architect", "fashion",
            "game", "3d", "cg", "video", "movie",
            "機械", "建築", "アパレル", "ゲーム", "映像", "施工",
        }
    )

    #: At least one of these must appear for the default design search.
    target_words: frozenset[str] = frozenset(
        {
            "web", "ui", "ux", "graphic", "designer", "frontend",
            "デザイン", "デザイナー", "フロントエンド",
        }
    )

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        keyword: str | None = None,
        location: str | None = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.keyword = keyword.strip() if keyword and keyword.strip() else None
        self.location = location.strip() if location and location.strip() else None

    # -- contract ------------------------------------------------------------

    @property
    @abstractmethod
    def platform(self) -> SourcePlatform:
        """The board this scraper reads."""

    @abstractmethod
    async def fetch_jobs(self) -> list[JobPosting]:
        """Fetch and parse every listing this run should see.

        Returns an empty list when the board simply has no results — that is
        different from a failure, which raises
        :class:`~jobreach.errors.ScraperError`.
        """

    # -- relevance -----------------------------------------------------------

    @staticmethod
    def is_design_title(title: str) -> bool:
        """Default (keyword-less) design filter: stop-words then target-words."""
        lower = title.lower()
        if any(stop in lower for stop in BaseScraper.stop_words):
            return False
        return any(target in lower for target in BaseScraper.target_words)

    def matches(self, title: str) -> bool:
        """Should *title* be kept for this run?

        - no keyword → :meth:`is_design_title` (default design feed)
        - keyword baked into the search URL → trust the server, keep everything
        - keyword applied client-side → keep titles containing any query token
        """
        if self.keyword is None:
            return self.is_design_title(title)
        if self.url_encodes_keyword:
            return True
        tokens = [token for token in self.keyword.lower().split() if token]
        lower = title.lower()
        return any(token in lower for token in tokens)

    # -- fetching ------------------------------------------------------------
    #
    # Scrapers never open a browser themselves: they describe the page work as
    # steps and this layer runs them on the selected backend. The two
    # implementations (Scrapling in a subprocess, Playwright in-process)
    # execute the same vocabulary, so a board cannot work on one and silently
    # break on the other.

    async def fetch(
        self,
        url: str,
        *,
        steps: Sequence[dict[str, Any]] = (),
        mode: str | None = None,
        wait_selector: str | None = None,
        timeout_ms: int | None = None,
        headless: bool | None = None,
    ) -> FetchResult:
        """Open *url*, run *steps*, and return the collected results.

        Tries :attr:`fetch_modes` in order; a mode that raises or comes back
        bot-blocked hands over to the next one, so a board that needs
        Cloudflare solving still has a plain-browser fallback.

        Raises:
            MissingDependencyError: no browser backend is installed.
            ScraperError: every mode failed, or the site served a challenge.
        """
        modes = (mode,) if mode else self.fetch_modes
        failures: list[str] = []
        blocked_once = False

        for candidate in modes:
            spec = build_spec(
                url,
                steps,
                mode=candidate,
                wait_selector=wait_selector,
                headless=self.headless if headless is None else headless,
                timeout_ms=timeout_ms or DEFAULT_TIMEOUT_MS,
            )
            try:
                payload = await self._dispatch(spec)
            except MissingDependencyError:
                raise
            except ScraperError as exc:
                failures.append(f"{candidate}: {exc}")
                continue

            result = FetchResult.from_payload(payload)
            if result.blocked:
                blocked_once = True
                failures.append(f"{candidate}: blocked by bot protection (no listings in the response)")
                logger.warning("fetch blocked", extra={"url": url, "mode": candidate})
                continue
            logger.info(
                "fetch ok",
                extra={"url": url, "mode": candidate, "status": result.status,
                       "elapsed_s": result.elapsed_s},
            )
            return result

        reason = "; ".join(failures) or "no fetch mode was attempted"
        if blocked_once:
            raise ScraperError(
                f"{self.platform.value}: the site served a bot challenge instead of "
                f"listings ({reason})"
            )
        raise ScraperError(f"could not load {url}: {reason}")

    async def evaluate_page(
        self,
        url: str,
        js: str,
        *,
        key: str = "cards",
        wait_selector: str | None = None,
        scrolls: int = 0,
        scroll_px: int = 800,
        settle_ms: int = 2_500,
        mode: str | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        """Open *url*, optionally scroll (lazy-loaded cards), run *js*, return its value.

        The common case: one page in, one ``evaluate`` out.
        """
        steps: list[dict[str, Any]] = []
        if scrolls:
            for _ in range(scrolls):
                steps.append(scroll(scroll_px, settle_ms=settle_ms))
        else:
            steps.append(wait(min(settle_ms, 1_500)))
        steps.append(evaluate(js, key))

        result = await self.fetch(
            url, steps=steps, mode=mode, wait_selector=wait_selector, timeout_ms=timeout_ms
        )
        return result.get(key)

    async def _dispatch(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Run *spec* on the selected backend (Scrapling subprocess / Playwright)."""
        backend = select_backend()
        if backend.name == "scrapling":
            return await asyncio.to_thread(run_scrapling, spec)
        return await self._run_playwright(spec)

    # -- playwright fallback -------------------------------------------------

    @asynccontextmanager
    async def browser_page(self) -> AsyncIterator[Any]:
        """Yield a stealth-configured, ja-JP Playwright page.

        The context is locale- and timezone-pinned to Japan so date and
        location strings render the way the parsers expect.
        """
        async_playwright, Stealth = require_playwright()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            try:
                context = await browser.new_context(
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                    user_agent=self.user_agent,
                    viewport={"width": 1280, "height": 900},
                )
                page = await context.new_page()
                if Stealth is not None:
                    await Stealth().apply_stealth_async(page)
                yield page
            finally:
                await browser.close()

    async def _run_playwright(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Execute the same step vocabulary with Playwright, in this process.

        Used when Scrapling is not installed but the plugin's own venv is. The
        semantics deliberately match ``drivers/scrapling_driver.py``: the same
        spec must produce the same shape of result on either backend.
        """
        results: dict[str, Any] = {}
        async with self.browser_page() as page:
            await self.goto(page, str(spec["url"]))
            if spec.get("wait_selector"):
                await page.wait_for_selector(
                    str(spec["wait_selector"]), state="attached", timeout=self.timeout_ms
                )
            for index, step in enumerate(spec.get("steps") or []):
                try:
                    settle_ms = int(step.get("wait_ms") or 0)
                    waited_inside = False

                    if "scroll" in step:
                        for _ in range(int(step.get("times") or 1)):
                            await page.evaluate(f"window.scrollBy(0, {int(step['scroll'])})")
                            if settle_ms:
                                await page.wait_for_timeout(settle_ms)
                        waited_inside = True
                    if "click" in step:
                        selector = str(step["click"])
                        if step.get("optional") and await page.locator(selector).count() == 0:
                            continue
                        await page.locator(selector).first.click()
                    if "wait_load" in step:
                        await page.wait_for_load_state(str(step["wait_load"]))
                    if "wait_selector" in step:
                        await page.wait_for_selector(
                            str(step["wait_selector"]),
                            state=str(step.get("state") or "attached"),
                            timeout=step.get("timeout_ms") or self.timeout_ms,
                        )
                    if "evaluate" in step:
                        results[str(step.get("key") or f"eval{index}")] = await page.evaluate(
                            str(step["evaluate"])
                        )
                    if "capture" in step:
                        results[str(step["capture"])] = await page.content()
                    if settle_ms and not waited_inside:
                        await page.wait_for_timeout(settle_ms)
                except Exception as exc:  # noqa: BLE001 — the step, not the fetch, failed
                    if step.get("optional"):
                        continue
                    raise ScraperError(
                        f"step {index} failed: {type(exc).__name__}: {exc}"
                    ) from exc

            html = ""
            try:
                html = await page.content()
            except Exception:  # noqa: BLE001 — a body we cannot read is still a page
                html = ""

            return {
                "ok": True,
                "status": None,  # Playwright exposes no status for the document
                "url": page.url,
                "title": await page.title(),
                "results": results,
                "blocked": False,
                "mode": spec.get("mode"),
                "html_len": len(html),
            }

    async def goto(self, page: Any, url: str, *, wait_until: str = "domcontentloaded") -> None:
        """Navigate with retry/backoff on transient network errors."""
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await page.goto(url, wait_until=wait_until, timeout=self.timeout_ms)
                return
            except Exception as exc:  # noqa: BLE001 — Playwright raises many types
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "navigation failed, retrying",
                        extra={"url": url, "attempt": attempt, "delay": delay},
                    )
                    await asyncio.sleep(delay)
        raise ScraperError(
            f"could not load {url} after {MAX_ATTEMPTS} attempts: {last_error}"
        ) from last_error


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
