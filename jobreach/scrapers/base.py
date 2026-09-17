"""Scraper strategy interface.

Every job board is one :class:`BaseScraper` subclass. The orchestrator in
:mod:`jobreach.pipeline` treats them polymorphically — it calls
``fetch_jobs()`` and never knows which site it is talking to.

Two implementation paths:

1. **JS-rendered boards** (Wantedly, Mynavi): subclass :class:`BaseScraper` and
   use :meth:`BaseScraper.browser_page`, which owns the Playwright lifecycle.
2. **CLI-driven boards** (LinkedIn via ``opencli``): subclass
   :class:`~jobreach.scrapers.cli_base.CliScraper` instead.

Playwright is imported **lazily, inside the methods that need it**. That is
what lets the rest of the plugin run on a bare Python interpreter: importing
``jobreach`` must never require a browser stack.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ..domain import JobPosting, SourcePlatform
from ..errors import MissingDependencyError, ScraperError
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


class BaseScraper(ABC):
    """Abstract strategy for one job board.

    Subclasses must implement :attr:`platform` and :meth:`fetch_jobs`.
    """

    #: Chrome-on-macOS UA. Boards serve the same markup to everyone, but a
    #: plausible UA avoids the crudest bot filters.
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )

    #: ``True`` when the board filters by keyword server-side (its search URL
    #: carries the query), so titles returned are already relevant and the
    #: client-side keyword filter must be skipped. Critical for cross-language
    #: queries: an English "engineer" search returns Japanese エンジニア titles
    #: that a literal substring match would wrongly reject.
    url_encodes_keyword: bool = False

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

    # -- browser plumbing ----------------------------------------------------

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
