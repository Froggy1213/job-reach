"""Abstract base class for job board scrapers (Strategy pattern).

Every concrete scraper is a strategy that knows how to fetch job
listings from one specific job board.  The orchestrator treats all
scrapers polymorphically -- it calls ``fetch_jobs()`` on each one
without knowing which site it targets.

Two implementation paths are supported:

1. **API-first boards**: Override ``fetch_jobs()`` directly and make
   an HTTP call (e.g. with ``httpx``).  ``parse_page()`` can raise
   ``NotImplementedError``.

2. **JS-rendered boards**: Override ``parse_page(page)`` with
   board-specific CSS/XPath selectors, then call
   ``self._scrape_with_playwright(url)`` from ``fetch_jobs()``.
   The base class handles browser lifecycle, locale, and timeouts.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from playwright.async_api import Page, async_playwright
from playwright_stealth import Stealth

from core.exceptions import ScraperError
from models.enums import SourcePlatform
from models.job_posting import JobPosting

logger = logging.getLogger("job_hunter.scraper")

# Retry configuration for transient failures.
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0  # seconds; doubles each attempt


class BaseScraper(ABC):
    """Abstract strategy for scraping a single job board.

    Subclasses MUST:
        - Implement the ``platform`` property
        - Implement ``fetch_jobs()`` (or ``parse_page()`` + use the helper)

    Subclasses MAY:
        - Call ``self._scrape_with_playwright(url)`` to reuse browser setup
        - Override ``self._user_agent`` to spoof a different UA string
    """

    # Default user agent — a recent Chrome on macOS.
    # Subclasses can override this class attribute.
    _user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 30_000,
        keyword: str | None = None,
        location: str | None = None,
    ) -> None:
        """Initialise the scraper.

        Args:
            headless: Whether to launch the browser in headless mode.
                Set to ``False`` during development to see what Playwright
                is doing.
            timeout_ms: Page navigation timeout in milliseconds.
            keyword: Optional free-text search query.  When ``None`` the
                scraper uses its built-in design-job defaults and the
                static ``is_target_job`` filter.  When set, the scraper
                searches for this query and ``matches`` filters by its
                tokens instead of the design word-list.
            location: Optional location override (site-specific slug, e.g.
                ``"tokyo"``).  ``None`` means the scraper's default;
                ``"any"``/``"all"`` disables location filtering where the
                board supports it.
        """
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._keyword = keyword.strip() if keyword and keyword.strip() else None
        self._location = location.strip() if location and location.strip() else None

    # ------------------------------------------------------------------
    # Job title filter (shared across all scrapers)
    # ------------------------------------------------------------------

    # Titles containing any of these words are REJECTED.
    _STOP_WORDS: set[str] = {
        "cad", "mechanical", "machine", "architect", "fashion",
        "game", "3d", "cg", "video", "movie",
        "機械", "建築", "アパレル", "ゲーム", "映像", "施工",
    }

    # Titles MUST contain at least one of these words to be ACCEPTED.
    _TARGET_WORDS: set[str] = {
        "web", "ui", "ux", "graphic", "designer", "frontend",
        "デザイン", "デザイナー", "フロントエンド",
    }

    @staticmethod
    def is_target_job(title: str) -> bool:
        """Return ``True`` if *title* is a target design job.

        Filtering logic (case-insensitive):

        1. If *title* contains any stop-word → reject immediately.
        2. If *title* contains any target word → accept.
        3. Otherwise → reject.
        """
        lower = title.lower()

        for stop in BaseScraper._STOP_WORDS:
            if stop in lower:
                return False

        for target in BaseScraper._TARGET_WORDS:
            if target in lower:
                return True

        return False

    # Whether a custom keyword is encoded into this board's search URL.
    # When ``True`` the site filters server-side and ``matches`` trusts
    # that result; when ``False`` ``matches`` applies a best-effort
    # client-side token filter on the title.  Boards with URL keyword
    # search override this to ``True``.
    _url_encodes_keyword: bool = False

    def matches(self, title: str) -> bool:
        """Return ``True`` if *title* should be kept for this scrape.

        Three modes:

        - **No keyword:** delegate to the built-in design-job filter
          ``is_target_job`` — preserves the original behaviour.
        - **Keyword encoded in the search URL** (``_url_encodes_keyword``):
          the board already searched server-side, so trust it and accept
          every card.  Crucial for cross-language queries — an English
          ``engineer`` query returns Japanese ``エンジニア`` titles that a
          literal substring match would wrongly reject.
        - **Keyword NOT in the URL:** best-effort — accept titles
          containing any whitespace-separated token of the keyword
          (case-insensitive).  The design stop-word list is not applied.
        """
        if self._keyword is None:
            return self.is_target_job(title)
        if self._url_encodes_keyword:
            return True
        lower = title.lower()
        tokens = [t for t in self._keyword.lower().split() if t]
        return any(token in lower for token in tokens)

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def platform(self) -> SourcePlatform:
        """Return the ``SourcePlatform`` enum value for this scraper.

        This property is read by the orchestrator for logging and
        attribution.  Each concrete scraper returns its own enum member.
        """
        ...

    @abstractmethod
    async def fetch_jobs(self) -> list[JobPosting]:
        """Fetch and parse all available job listings.

        This is the main entry point called by the orchestrator.
        The returned list may be empty if no new listings are found,
        but an empty list is distinct from a scraper failure (which
        raises ``ScraperError``).

        Returns:
            A list of validated ``JobPosting`` domain models, each with
            the scraper's ``platform`` already set.

        Raises:
            ScraperError: If the fetch or parse operation fails.
        """
        ...

    async def parse_page(self, page: Page) -> list[JobPosting]:
        """Parse a fully-loaded Playwright ``Page`` and extract job listings.

        Called by ``_scrape_with_playwright`` after navigation completes.
        Subclasses that use the ``_scrape_with_playwright`` helper should
        override this method with board-specific CSS/XPath selectors.

        Subclasses that manage Playwright lifecycle themselves (and
        override ``fetch_jobs()`` directly) do not need to implement this.

        Args:
            page: A Playwright ``Page`` that has already navigated to the
                target URL and waited for ``networkidle``.

        Returns:
            A list of ``JobPosting`` domain models extracted from the page.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement parse_page() or fetch_jobs()"
        )

    # ------------------------------------------------------------------
    # Shared Playwright helper
    # ------------------------------------------------------------------

    async def _scrape_with_playwright(self, url: str) -> list[JobPosting]:
        """Open *url* in a headless Chromium browser and delegate to
        ``parse_page()``.

        This helper encapsulates the Playwright lifecycle (launch →
        create context → navigate → parse → close) so subclasses don't
        need to repeat browser setup logic.

        The browser context is configured with Japanese locale and
        Tokyo timezone so date strings render correctly for
        ``parse_page()`` selectors.

        Includes automatic retry with exponential backoff for transient
        network failures (up to ``_MAX_RETRIES`` attempts).

        Args:
            url: The job board URL to navigate to.

        Returns:
            The result of ``self.parse_page(page)``.

        Raises:
            ScraperError: Wraps any Playwright or parsing exception after
                all retries are exhausted.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await self._single_scrape(url)
            except ScraperError:
                raise  # parse_page errors are not retryable
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "Scrape attempt %d/%d failed, retrying in %.1fs",
                        attempt,
                        _MAX_RETRIES,
                        delay,
                        extra={"url": url, "error": str(exc)},
                    )
                    await asyncio.sleep(delay)

        raise ScraperError(
            f"Playwright scrape failed for {url} after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    async def _single_scrape(self, url: str) -> list[JobPosting]:
        """Execute a single scrape attempt (no retry)."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            try:
                context = await browser.new_context(
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                    user_agent=self._user_agent,
                )
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)
                logger.info("Navigating", extra={"url": url})
                await page.goto(url, wait_until="networkidle", timeout=self._timeout_ms)
                return await self.parse_page(page)
            except Exception as exc:
                raise ScraperError(
                    f"Playwright scrape failed for {url}: {exc}"
                ) from exc
            finally:
                await browser.close()

